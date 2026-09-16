"""证券池 SQLite 建表、查询和批量写入函数。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .processor import now_text


MASTER_COLUMNS = (
    "stock_code",
    "exchange",
    "security_kind",
    "list_date",
    "initial_name",
    "trade_unit",
    "min_price_tick",
    "price_precision",
    "first_seen_date",
    "created_at",
    "updated_at",
)

CURRENT_COLUMNS = (
    "stock_code",
    "data_date",
    "stock_name",
    "is_active",
    "is_all_a",
    "is_main_board",
    "is_gem",
    "is_star",
    "is_bj_a",
    "is_hs300",
    "is_zz500",
    "is_zz1000",
    "is_a500",
    "is_stock_connect",
    "is_marginable",
    "is_st",
    "is_delisting_board",
    "is_suspended",
    "has_convertible_bond",
    "industry_code",
    "industry_name",
    "region_code",
    "region_name",
    "total_shares_10k",
    "float_shares_10k",
    "free_float_shares_10k",
    "total_market_cap_100m",
    "float_market_cap_100m",
    "turnover_rate",
    "amount_prev_1d_10k",
    "pe_ttm",
    "pb_mrq",
    "dividend_yield",
    "is_tradable",
    "exclusion_reasons",
    "updated_at",
)

SNAPSHOT_COLUMNS = (
    "snapshot_date",
    *(column for column in CURRENT_COLUMNS if column not in {"data_date", "updated_at"}),
    "archived_at",
)

SECTOR_COLUMNS = (
    "snapshot_date",
    "stock_code",
    "sector_code",
    "sector_name",
    "sector_type",
    "source",
    "archived_at",
)

IPO_COLUMNS = (
    "security_code",
    "security_name",
    "issue_type",
    "subscription_date",
    "subscription_price",
    "subscription_code",
    "max_subscription",
    "issue_pe",
    "updated_at",
)

BOND_COLUMNS = (
    "bond_code",
    "underlying_code",
    "conversion_price",
    "remaining_size",
    "maturity_date",
    "bond_price",
    "stock_price",
    "premium_rate",
    "conversion_value",
    "data_date",
    "updated_at",
)

ETF_COLUMNS = (
    "index_code",
    "etf_code",
    "etf_name",
    "current_price",
    "previous_close",
    "iopv",
    "shares_10k",
    "size_100m",
    "data_date",
    "updated_at",
)

ETF_SNAPSHOT_COLUMNS = (
    "snapshot_date",
    "index_code",
    "etf_code",
    "etf_name",
    "current_price",
    "previous_close",
    "iopv",
    "shares_10k",
    "size_100m",
    "premium_discount_pct",
    "archived_at",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    # 打开 SQLite 数据库并设置本项目需要的连接参数。
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db(connection: sqlite3.Connection, schema_path: str | Path | None = None) -> None:
    # 执行可重复运行的 SQLite 建表脚本。
    path = Path(schema_path) if schema_path else Path(__file__).with_name("schema.sql")
    connection.executescript(path.read_text(encoding="utf-8"))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(etf_daily)")}
    if columns and "forward_factor" not in columns:
        connection.execute("ALTER TABLE etf_daily ADD COLUMN forward_factor REAL")
    connection.commit()


def start_log(
    connection: sqlite3.Connection,
    data_date: str,
    task_type: str,
) -> int:
    # 创建任务运行日志并返回日志主键。
    cursor = connection.execute(
        "INSERT INTO fetch_log(data_date, task_type, status, started_at) "
        "VALUES (?, ?, 'RUNNING', ?)",
        (data_date, task_type, now_text()),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_log(
    connection: sqlite3.Connection,
    log_id: int,
    status: str,
    stock_count: int = 0,
    error_message: str | None = None,
    commit: bool = True,
) -> None:
    # 更新任务运行日志的最终状态和统计值。
    connection.execute(
        "UPDATE fetch_log SET status=?, finished_at=?, stock_count=?, "
        "error_message=? WHERE id=?",
        (status, now_text(), stock_count, error_message, log_id),
    )
    if commit:
        connection.commit()


def load_master_codes(connection: sqlite3.Connection) -> set[str]:
    # 返回股票主表中所有历史发现过的股票代码。
    cursor = connection.execute("SELECT stock_code FROM stock_master")
    return {str(row["stock_code"]) for row in cursor}


def load_current_rows(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    # 读取当前证券池宽表并按股票代码建立索引。
    cursor = connection.execute("SELECT * FROM stock_current")
    return {str(row["stock_code"]): dict(row) for row in cursor}


def _row_values(
    row: Mapping[str, Any],
    columns: Sequence[str],
) -> tuple[Any, ...]:
    # 按指定列顺序提取一条数据库记录的值。
    return tuple(row.get(column) for column in columns)


def _insert_sql(
    table: str,
    columns: Sequence[str],
    conflict_columns: Sequence[str] = (),
) -> str:
    # 生成带可选冲突更新逻辑的 SQLite 插入语句。
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({names}) VALUES ({placeholders})"
    if not conflict_columns:
        return sql
    update_columns = [column for column in columns if column not in conflict_columns]
    updates = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
    conflicts = ", ".join(conflict_columns)
    return f"{sql} ON CONFLICT ({conflicts}) DO UPDATE SET {updates}"


def upsert_stock_master(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    # 批量新增股票主表并更新允许修正的稳定属性。
    row_list = list(rows)
    if not row_list:
        return
    names = ", ".join(MASTER_COLUMNS)
    placeholders = ", ".join("?" for _ in MASTER_COLUMNS)
    sql = (
        f"INSERT INTO stock_master ({names}) VALUES ({placeholders}) "
        "ON CONFLICT (stock_code) DO UPDATE SET "
        "exchange=excluded.exchange, "
        "security_kind=COALESCE(excluded.security_kind, stock_master.security_kind), "
        "list_date=COALESCE(excluded.list_date, stock_master.list_date), "
        "trade_unit=COALESCE(excluded.trade_unit, stock_master.trade_unit), "
        "min_price_tick=COALESCE(excluded.min_price_tick, stock_master.min_price_tick), "
        "price_precision=COALESCE(excluded.price_precision, stock_master.price_precision), "
        "updated_at=excluded.updated_at"
    )
    connection.executemany(
        sql,
        (_row_values(row, MASTER_COLUMNS) for row in row_list),
    )


def replace_stock_current(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    # 批量 UPSERT 全部历史股票的当前证券池记录。
    row_list = list(rows)
    if not row_list:
        raise ValueError("当前证券池记录不能为空")
    sql = _insert_sql("stock_current", CURRENT_COLUMNS, ("stock_code",))
    connection.executemany(
        sql,
        (_row_values(row, CURRENT_COLUMNS) for row in row_list),
    )


def _snapshot_row(
    row: Mapping[str, Any],
    data_date: str,
) -> dict[str, Any]:
    # 将当前宽表记录转换为每日快照记录。
    snapshot = dict(row)
    snapshot.pop("data_date", None)
    snapshot.pop("updated_at", None)
    snapshot["snapshot_date"] = data_date
    snapshot["archived_at"] = now_text()
    return snapshot


def save_stock_snapshot(
    connection: sqlite3.Connection,
    data_date: str,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    # 替换指定日期的全部股票宽表快照。
    row_list = list(rows)
    connection.execute(
        "DELETE FROM stock_snapshot WHERE snapshot_date=?",
        (data_date,),
    )
    sql = _insert_sql("stock_snapshot", SNAPSHOT_COLUMNS)
    snapshots = (_snapshot_row(row, data_date) for row in row_list)
    connection.executemany(
        sql,
        (_row_values(row, SNAPSHOT_COLUMNS) for row in snapshots),
    )


def save_sector_snapshot(
    connection: sqlite3.Connection,
    data_date: str,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    # 替换指定日期的股票板块关系快照。
    row_list = list(rows)
    connection.execute(
        "DELETE FROM stock_sector_snapshot WHERE snapshot_date=?",
        (data_date,),
    )
    if not row_list:
        return
    connection.executemany(
        _insert_sql("stock_sector_snapshot", SECTOR_COLUMNS),
        (_row_values(row, SECTOR_COLUMNS) for row in row_list),
    )


def upsert_ipo_info(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]] | None,
) -> None:
    # 更新新股新债申购日历，空值表示本次获取失败并保留旧数据。
    if rows is None:
        return
    row_list = list(rows)
    if not row_list:
        return
    sql = _insert_sql(
        "ipo_info",
        IPO_COLUMNS,
        ("security_code", "subscription_date", "issue_type"),
    )
    connection.executemany(sql, (_row_values(row, IPO_COLUMNS) for row in row_list))


def replace_convertible_bonds(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]] | None,
) -> None:
    # 替换可转债当前表，获取失败时保留旧版本。
    if rows is None:
        return
    row_list = list(rows)
    connection.execute("DELETE FROM convertible_bond_info")
    if row_list:
        connection.executemany(
            _insert_sql("convertible_bond_info", BOND_COLUMNS),
            (_row_values(row, BOND_COLUMNS) for row in row_list),
        )


def replace_etfs(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]] | None,
) -> None:
    # 替换 ETF 当前表，获取失败时保留旧版本。
    if rows is None:
        return
    row_list = list(rows)
    connection.execute("DELETE FROM etf_info")
    if row_list:
        connection.executemany(
            _insert_sql("etf_info", ETF_COLUMNS),
            (_row_values(row, ETF_COLUMNS) for row in row_list),
        )


def save_etf_snapshot(
    connection: sqlite3.Connection,
    data_date: str,
    rows: Iterable[Mapping[str, Any]] | None,
) -> None:
    # 归档 ETF—指数关系和当日净值、份额、规模快照。
    if rows is None:
        return
    archived_at = now_text()
    snapshot_rows = []
    for row in rows:
        current_price = row.get("current_price")
        iopv = row.get("iopv")
        premium_discount_pct = None
        if current_price is not None and iopv not in {None, 0}:
            premium_discount_pct = (float(current_price) / float(iopv) - 1.0) * 100.0
        snapshot_rows.append({
            "snapshot_date": data_date,
            **{column: row.get(column) for column in ETF_COLUMNS if column not in {"data_date", "updated_at"}},
            "premium_discount_pct": premium_discount_pct,
            "archived_at": archived_at,
        })
    if snapshot_rows:
        connection.executemany(
            _insert_sql(
                "etf_snapshot",
                ETF_SNAPSHOT_COLUMNS,
                ("snapshot_date", "index_code", "etf_code"),
            ),
            (_row_values(row, ETF_SNAPSHOT_COLUMNS) for row in snapshot_rows),
        )


def save_daily_data(
    connection: sqlite3.Connection,
    data_date: str,
    master_rows: Iterable[Mapping[str, Any]],
    current_rows: Iterable[Mapping[str, Any]],
    sector_rows: Iterable[Mapping[str, Any]],
    ipo_rows: Iterable[Mapping[str, Any]] | None,
    bond_rows: Iterable[Mapping[str, Any]] | None,
    etf_rows: Iterable[Mapping[str, Any]] | None,
    log_id: int,
) -> None:
    # 在一个事务内更新全部当前数据、每日快照和成功日志。
    master_list = list(master_rows)
    current_list = list(current_rows)
    sector_list = list(sector_rows)
    with connection:
        upsert_stock_master(connection, master_list)
        replace_stock_current(connection, current_list)
        save_stock_snapshot(connection, data_date, current_list)
        save_sector_snapshot(connection, data_date, sector_list)
        upsert_ipo_info(connection, ipo_rows)
        replace_convertible_bonds(connection, bond_rows)
        save_etf_snapshot(connection, data_date, etf_rows)
        replace_etfs(connection, etf_rows)
        finish_log(
            connection,
            log_id,
            "SUCCESS",
            stock_count=len(current_list),
            commit=False,
        )


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    # 返回所有证券池业务表的当前记录数量。
    tables = (
        "stock_master",
        "stock_current",
        "stock_snapshot",
        "stock_sector_snapshot",
        "ipo_info",
        "convertible_bond_info",
        "etf_info",
        "fetch_log",
    )
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts
