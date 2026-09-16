"""将旧数据库安全拆分为市场、研究、账户和日报数据库。"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from invest.core.settings import Settings


MARKET_TABLES = frozenset({
    "convertible_bond_info", "etf_action", "etf_adjustment", "etf_calendar",
    "etf_classification", "etf_daily", "etf_daily_provenance", "etf_data_sync_state",
    "etf_identity", "etf_info", "etf_market_daily", "etf_master", "etf_rule",
    "etf_snapshot", "etf_universe_validation", "etf_validation", "fetch_log", "ipo_info",
    "stock_capital_daily", "stock_corporate_action", "stock_current", "stock_daily_bar",
    "stock_data_sync_state", "stock_financial_report", "stock_master", "stock_sector_snapshot",
    "stock_snapshot", "stock_trade_daily",
})
RESEARCH_TABLES = frozenset({
    "strategy_run", "factor_score_daily", "portfolio_target", "backtest_nav", "backtest_trade",
})
REPORT_TABLES = frozenset({"observations", "reports", "runs"})
ETF_RESULT_TABLES = frozenset({"research_run"})
ALL_SOURCE_TABLES = MARKET_TABLES | RESEARCH_TABLES


def _tables(connection: sqlite3.Connection) -> set[str]:
    # 返回用户业务表，排除 SQLite 内部表。
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    # 用只读方式打开既有数据库，避免检查过程写入数据。
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _source_paths(settings: Settings) -> dict[str, Path]:
    # 返回当前项目中待迁移的旧数据库位置。
    return {
        "market": settings.root / "data/security_pool.db",
        "etf_results": settings.root / "data/etf_strategy.db",
        "reports": settings.root / "data/daily_report.db",
    }


def check_sources(settings: Settings) -> dict[str, object]:
    # 检查旧库表集合和完整性，不修改任何文件。
    details: dict[str, object] = {}
    expected = {"market": ALL_SOURCE_TABLES, "etf_results": ETF_RESULT_TABLES,
                "reports": REPORT_TABLES}
    for name, path in _source_paths(settings).items():
        if not path.exists():
            raise FileNotFoundError(f"缺少迁移源数据库: {path}")
        with _connect_readonly(path) as connection:
            tables = _tables(connection)
            unknown = tables - expected[name]
            missing = expected[name] - tables
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if unknown or missing or integrity != "ok":
                raise RuntimeError(
                    f"{name} 不符合迁移清单: unknown={sorted(unknown)}, "
                    f"missing={sorted(missing)}, integrity={integrity}"
                )
            details[name] = {"path": str(path), "tables": sorted(tables),
                             "rows": {table: connection.execute(
                                 f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                                      for table in sorted(tables)}}
    return details


def _backup(source: Path, destination: Path) -> None:
    # 使用 SQLite 备份 API 制作一致性数据库副本。
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _connect_readonly(source) as origin, sqlite3.connect(destination) as copy:
        origin.backup(copy)


def _copy_tables(
    source: Path,
    target: Path,
    tables: frozenset[str],
    append: bool = False,
) -> None:
    # 按原始建表语句、数据和索引复制指定表，保留结构与主键。
    if target.exists() and target.stat().st_size and not append:
        raise FileExistsError(f"目标数据库已存在，拒绝覆盖: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _connect_readonly(source) as origin, sqlite3.connect(target) as destination:
        destination.execute("PRAGMA foreign_keys=OFF")
        for table in sorted(tables):
            row = origin.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None or row[0] is None:
                raise RuntimeError(f"未找到建表语句: {table}")
            destination.execute(row[0])
            columns = [item[1] for item in origin.execute(f'PRAGMA table_info("{table}")')]
            quoted = ", ".join(f'"{column}"' for column in columns)
            marks = ", ".join("?" for _ in columns)
            rows = origin.execute(f'SELECT {quoted} FROM "{table}"')
            destination.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({marks})', rows)
        for kind in ("index", "trigger"):
            objects = origin.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=? AND sql IS NOT NULL",
                (kind,)).fetchall()
            for sql, in objects:
                destination.execute(sql)
        if not append:
            destination.execute(
                "CREATE TABLE schema_migration "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            destination.execute(
                "INSERT INTO schema_migration VALUES (1, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
        destination.execute("PRAGMA foreign_keys=ON")


def _create_accounts(path: Path) -> None:
    # 创建独立账户账本及其迁移版本记录。
    if path.exists() and path.stat().st_size:
        raise FileExistsError(f"目标数据库已存在，拒绝覆盖: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = """
    CREATE TABLE manual_account (
      account_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, cash REAL NOT NULL,
      holdings_json TEXT NOT NULL, initial_json TEXT NOT NULL);
    CREATE TABLE manual_fill (
      account_id TEXT NOT NULL, fill_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      PRIMARY KEY(account_id, fill_id));
    CREATE TABLE manual_plan (plan_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
    CREATE TABLE manual_event (
      account_id TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      PRIMARY KEY(account_id, event_id));
    CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    """
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO schema_migration VALUES (1, ?)",
                           (datetime.now(timezone.utc).isoformat(),))


def migrate(settings: Settings) -> dict[str, object]:
    # 备份旧库后创建四个新库；任何预存在目标都会阻止执行。
    source_status = check_sources(settings)
    destinations = settings.databases
    existing = [path for path in destinations.values() if path.exists()]
    if existing:
        raise FileExistsError(f"目标数据库已存在，拒绝覆盖: {existing}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = settings.path(settings.config["paths"]["backup_dir"]) / f"pre_migration_{stamp}"
    for name, source in _source_paths(settings).items():
        _backup(source, backup_root / f"{name}.db")
    _copy_tables(_source_paths(settings)["market"], destinations["market"], MARKET_TABLES)
    _copy_tables(_source_paths(settings)["market"], destinations["research"], RESEARCH_TABLES)
    _copy_tables(
        _source_paths(settings)["etf_results"],
        destinations["research"],
        ETF_RESULT_TABLES,
        append=True,
    )
    _copy_tables(_source_paths(settings)["reports"], destinations["reports"], REPORT_TABLES)
    _create_accounts(destinations["accounts"])
    return {"backup": str(backup_root), "sources": source_status,
            "destinations": {name: str(path) for name, path in destinations.items()}}


def _digest(connection: sqlite3.Connection, table: str) -> str:
    # 基于插入顺序计算表数据摘要，用于迁移后的逐行一致性核对。
    digest = hashlib.sha256()
    for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify(settings: Settings) -> dict[str, object]:
    # 核对新旧表行数、数据摘要和 SQLite 完整性。
    check_sources(settings)
    pairs = (
        (_source_paths(settings)["market"], settings.databases["market"], MARKET_TABLES),
        (_source_paths(settings)["market"], settings.databases["research"], RESEARCH_TABLES),
        (_source_paths(settings)["etf_results"], settings.databases["research"], ETF_RESULT_TABLES),
        (_source_paths(settings)["reports"], settings.databases["reports"], REPORT_TABLES),
    )
    checked: dict[str, int] = {}
    for source, target, tables in pairs:
        if not target.exists():
            raise FileNotFoundError(f"缺少迁移目标数据库: {target}")
        with _connect_readonly(source) as old, _connect_readonly(target) as new:
            if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(f"目标数据库损坏: {target}")
            for table in tables:
                old_count = old.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                new_count = new.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if old_count != new_count or _digest(old, table) != _digest(new, table):
                    raise RuntimeError(f"迁移核对失败: {table}")
                checked[table] = old_count
    with _connect_readonly(settings.databases["accounts"]) as accounts:
        if accounts.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("账户数据库完整性检查失败")
    return {"status": "ok", "tables": checked}
