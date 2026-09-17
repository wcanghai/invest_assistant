"""从一致性快照拆分数据库，验证成功后发布并保存可追溯清单。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
ACCOUNT_TABLES = frozenset({"manual_account", "manual_fill", "manual_plan", "manual_event"})
ALL_SOURCE_TABLES = MARKET_TABLES | RESEARCH_TABLES
GROUPS = (
    ("market", "market", MARKET_TABLES),
    ("market", "research", RESEARCH_TABLES),
    ("etf_results", "research", ETF_RESULT_TABLES),
    ("reports", "reports", REPORT_TABLES),
)


def _quote(name: str) -> str:
    # 引用已从 SQLite 元数据读取的标识符。
    return '"' + name.replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection) -> set[str]:
    # 列出业务表，排除 SQLite 自身的内部表。
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    # 只读打开已有文件，拒绝隐式创建空库。
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _source_paths(settings: Settings) -> dict[str, Path]:
    # 固定旧库清单，不能将新库误识别为迁移来源。
    return {
        "market": settings.root / "data/security_pool.db",
        "etf_results": settings.root / "data/etf_strategy.db",
        "reports": settings.root / "data/daily_report.db",
    }


def _check_integrity(connection: sqlite3.Connection) -> None:
    # 检查全部页的结构及外键，数据和索引另由迁移比较覆盖。
    errors = connection.execute("PRAGMA quick_check").fetchall()
    if errors != [("ok",)]:
        raise RuntimeError(f"SQLite quick_check 失败: {errors[:10]}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchmany(10)
    if violations:
        raise RuntimeError(f"SQLite 外键校验失败: {violations}")


def _check_paths(paths: dict[str, Path], integrity: bool) -> dict[str, object]:
    # 先核对表清单，按需检查快照完整性，始终关闭数据库连接。
    expected = {"market": ALL_SOURCE_TABLES, "etf_results": ETF_RESULT_TABLES,
                "reports": REPORT_TABLES}
    result = {}
    for name, path in paths.items():
        if not path.is_file() or not path.stat().st_size:
            raise FileNotFoundError(f"源数据库不存在或为空: {path}")
        with closing(_connect_readonly(path)) as connection:
            actual = _tables(connection)
            unknown, missing = actual - expected[name], expected[name] - actual
            if unknown or missing:
                raise RuntimeError(
                    f"{name}: unknown={sorted(unknown)}, missing={sorted(missing)}"
                )
            if integrity:
                _check_integrity(connection)
            result[name] = {"path": str(path), "tables": sorted(actual),
                            "integrity": "quick_check+foreign_key_check" if integrity else None}
    return result


def check_sources(settings: Settings) -> dict[str, object]:
    # 只读检查旧库结构、SQLite 页和外键，不创建目录或目标库。
    return _check_paths(_source_paths(settings), integrity=True)


def _backup(source: Path, destination: Path) -> None:
    # SQLite 备份包含已提交的 WAL 内容，并显式关闭两侧句柄。
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"备份已存在: {destination}")
    with closing(_connect_readonly(source)) as origin:
        with closing(sqlite3.connect(destination)) as target:
            origin.backup(target, pages=8192)


def _version(connection: sqlite3.Connection) -> None:
    # 初始化当前目标库的独立版本记录。
    connection.execute(
        "CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO schema_migration VALUES (1, ?)",
                       (datetime.now(timezone.utc).isoformat(),))


def _copy_tables(
    source: Path,
    target: Path,
    tables: frozenset[str],
    append: bool = False,
) -> None:
    # 用只读快照执行整表复制，原样保留结构、索引、触发器和自增上限。
    if target.exists() and not append:
        raise FileExistsError(f"目标数据库已存在，拒绝覆盖: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target.as_uri(), uri=True)) as destination:
        destination.execute("ATTACH DATABASE ? AS snapshot", (source.as_uri() + "?mode=ro",))
        with destination:
            destination.execute("BEGIN")
            for table in sorted(tables):
                print(f"COPY {target.name}: {table}", flush=True)
                row = destination.execute(
                    "SELECT sql FROM snapshot.sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"缺少源表: {table}")
                destination.execute(row[0])
                destination.execute(
                    f"INSERT INTO main.{_quote(table)} SELECT * FROM snapshot.{_quote(table)}"
                )
                if "AUTOINCREMENT" in row[0].upper():
                    sequence = destination.execute(
                        "SELECT seq FROM snapshot.sqlite_sequence WHERE name=?", (table,)
                    ).fetchone()
                    if sequence is not None:
                        destination.execute("DELETE FROM main.sqlite_sequence WHERE name=?",
                                            (table,))
                        destination.execute("INSERT INTO main.sqlite_sequence VALUES (?,?)",
                                            (table, sequence[0]))
            for table in sorted(tables):
                objects = destination.execute(
                    "SELECT sql FROM snapshot.sqlite_master WHERE type IN ('index','trigger') "
                    "AND tbl_name=? AND sql IS NOT NULL ORDER BY type,name", (table,)
                ).fetchall()
                for sql, in objects:
                    destination.execute(sql)
            if not append:
                _version(destination)


def _create_accounts(path: Path) -> None:
    # 初始化空账户库，绝不覆盖已有账户。
    if path.exists():
        raise FileExistsError(f"目标数据库已存在: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.executescript("""
            CREATE TABLE manual_account (
              account_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, cash REAL NOT NULL,
              holdings_json TEXT NOT NULL, initial_json TEXT NOT NULL);
            CREATE TABLE manual_fill (
              account_id TEXT NOT NULL, fill_id TEXT NOT NULL, payload_json TEXT NOT NULL,
              PRIMARY KEY(account_id,fill_id));
            CREATE TABLE manual_plan (plan_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
            CREATE TABLE manual_event (
              account_id TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
              PRIMARY KEY(account_id,event_id));
            """)
            _version(connection)


def _fingerprint(connection: sqlite3.Connection, table: str) -> dict[str, object]:
    # 按主键稳定排序并计算全部行摘要，支持无 rowid 表。
    columns = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    keys = keys or [row[1] for row in columns]
    order = ','.join(_quote(key) for key in keys)
    digest, count = hashlib.sha256(), 0
    cursor = connection.execute(f"SELECT * FROM {_quote(table)} ORDER BY {order}")
    for row in cursor:
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    schema = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
        "ORDER BY type,name", (table,)
    ).fetchall()
    sequence = None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
    ).fetchone():
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
        ).fetchone()
    return {"rows": count, "sha256": digest.hexdigest(), "schema": schema,
            "sequence": sequence}


def _verify_paths(sources: dict[str, Path], targets: dict[str, Path]) -> dict[str, object]:
    # 独立核对全部行、DDL、自增序列和外键，确保分库与固定快照一致。
    expected = {"market": MARKET_TABLES, "research": RESEARCH_TABLES | ETF_RESULT_TABLES,
                "reports": REPORT_TABLES, "accounts": ACCOUNT_TABLES}
    for name, path in targets.items():
        with closing(_connect_readonly(path)) as connection:
            if _tables(connection) != expected[name] | {"schema_migration"}:
                raise RuntimeError(f"目标表清单不符: {name}")
            _check_integrity(connection)
            versions = connection.execute("SELECT version FROM schema_migration").fetchall()
            if versions != [(1,)]:
                raise RuntimeError(f"目标版本不符: {name}")
    checked = {}
    for source, target, tables in GROUPS:
        with closing(_connect_readonly(sources[source])) as old:
            with closing(_connect_readonly(targets[target])) as new:
                for table in sorted(tables):
                    print(f"VERIFY {target}: {table}", flush=True)
                    before, after = _fingerprint(old, table), _fingerprint(new, table)
                    if before != after:
                        raise RuntimeError(f"数据或结构核对失败: {target}.{table}")
                    checked[f"{target}.{table}"] = before
    with closing(_connect_readonly(targets["accounts"])) as connection:
        for table in ACCOUNT_TABLES:
            if connection.execute(f"SELECT 1 FROM {_quote(table)} LIMIT 1").fetchone():
                raise RuntimeError("本次迁移预期为空账户库，发现账户记录")
    return {"status": "ok", "tables": checked}


def _manifest_path(settings: Settings) -> Path:
    # 清单与目标目录并列，只有全部验证发布成功后才写入。
    return settings.path(settings.config["paths"]["database_dir"]) / "migration_manifest.json"


def migrate(settings: Settings) -> dict[str, object]:
    # 先快照，再在隔离目录构建和验证，最后发布四库与完成清单。
    destinations = settings.databases
    existing = [str(path) for path in destinations.values() if path.exists()]
    if existing or _manifest_path(settings).exists():
        raise FileExistsError(f"拒绝覆盖已有目标或迁移清单: {existing}")
    sources = _source_paths(settings)
    _check_paths(sources, integrity=False)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + '_' + uuid4().hex[:8]
    folder = settings.path(settings.config["paths"]["backup_dir"]) / f"pre_migration_{stamp}"
    snapshots = {key: folder / f"{key}.db" for key in sources}
    for name, source in sources.items():
        print(f"BACKUP {name}: {source}", flush=True)
        _backup(source, snapshots[name])
    _check_paths(snapshots, integrity=True)
    staged = {name: folder / "staging" / path.name for name, path in destinations.items()}
    for source, target, tables in GROUPS:
        _copy_tables(snapshots[source], staged[target], tables, append=staged[target].exists())
    _create_accounts(staged["accounts"])
    result = _verify_paths(snapshots, staged)
    manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "sources": {name: str(path) for name, path in snapshots.items()},
                "targets": {name: str(path) for name, path in destinations.items()},
                "verification": result}
    for name, target in destinations.items():
        if target.exists():
            raise FileExistsError(f"发布前发现目标已被其他进程创建: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staged[name].rename(target)
    manifest_path = _manifest_path(settings)
    temporary = manifest_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    return {"status": "ok", "backup": str(folder), "manifest": str(manifest_path),
            "verified_tables": len(result["tables"])}


def verify(settings: Settings) -> dict[str, object]:
    # 核对固定备份与已发布数据库，不重复扫描持续变化的旧库。
    targets = settings.databases
    for name, path in targets.items():
        if not path.is_file() or not path.stat().st_size:
            raise FileNotFoundError(f"目标库缺失或为空: {name}: {path}")
    manifest = json.loads(_manifest_path(settings).read_text(encoding="utf-8"))
    if manifest["targets"] != {name: str(path) for name, path in targets.items()}:
        raise RuntimeError("配置目标与迁移清单不符")
    sources = {name: Path(path) for name, path in manifest["sources"].items()}
    return _verify_paths(sources, targets)
