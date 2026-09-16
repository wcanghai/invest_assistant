"""股票详细数据的 SQLite 建表、查询和批量写入。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from security_pool import db as pool_db


BAR_COLUMNS = (
    "stock_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount_10k",
    "forward_factor",
    "pre_close",
    "pct_change",
    "is_trading",
    "updated_at",
)

CAPITAL_COLUMNS = (
    "stock_code",
    "trade_date",
    "total_shares",
    "float_shares",
    "updated_at",
)

ACTION_COLUMNS = (
    "stock_code",
    "event_date",
    "action_type",
    "cash_bonus",
    "allot_price",
    "share_bonus",
    "allotment",
    "updated_at",
)

TRADE_COLUMNS = (
    "stock_code",
    "trade_date",
    "shareholder_count",
    "financing_balance_10k",
    "securities_lending_balance_shares",
    "northbound_holding_shares",
    "financing_buy_10k",
    "financing_repay_10k",
    "financing_net_buy_10k",
    "total_market_cap_10k",
    "dividend_yield_pct",
    "limit_status",
    "limit_order_amount_10k",
    "pledge_ratio_pct",
    "market_popularity_rank",
    "industry_popularity_rank",
    "raw_metrics_json",
    "updated_at",
)

FINANCIAL_COLUMNS = (
    "stock_code",
    "report_date",
    "announce_date",
    "basic_eps",
    "deduct_eps",
    "book_value_per_share",
    "roe_pct",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "operating_cash_flow",
    "net_profit",
    "revenue",
    "operating_profit",
    "parent_net_profit",
    "deduct_net_profit",
    "revenue_growth_pct",
    "net_profit_growth_pct",
    "gross_margin_pct",
    "debt_ratio_pct",
    "total_shares",
    "float_a_shares",
    "shareholder_count",
    "raw_values_json",
    "updated_at",
)

DETAIL_TABLES = (
    "stock_daily_bar",
    "stock_capital_daily",
    "stock_corporate_action",
    "stock_trade_daily",
    "stock_financial_report",
)

DOMAIN_DATE_SOURCES = {
    "bar": ("stock_daily_bar", "trade_date"),
    "capital": ("stock_capital_daily", "trade_date"),
    "action": ("stock_corporate_action", "event_date"),
    "trade": ("stock_trade_daily", "trade_date"),
    "financial": ("stock_financial_report", "announce_date"),
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    # 使用证券池统一连接参数打开 SQLite 数据库。
    return pool_db.connect(db_path)


def init_db(connection: sqlite3.Connection) -> None:
    # 先创建证券池基础表，再创建股票详细数据表。
    pool_db.init_db(connection)
    schema_path = Path(__file__).with_name("schema.sql")
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    connection.commit()


def start_log(
    connection: sqlite3.Connection,
    data_date: str,
    task_type: str,
) -> int:
    # 在共享获取日志表中创建股票详细数据任务。
    return pool_db.start_log(connection, data_date, task_type)


def finish_log(
    connection: sqlite3.Connection,
    log_id: int,
    status: str,
    stock_count: int,
    error_message: str | None = None,
) -> None:
    # 更新股票详细数据任务日志。
    pool_db.finish_log(
        connection,
        log_id,
        status,
        stock_count=stock_count,
        error_message=error_message,
    )


def load_stock_scope(
    connection: sqlite3.Connection,
    stock_codes: Sequence[str] | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    # 从唯一股票主表读取代码、上市日和当前活动状态。
    sql = (
        "SELECT m.stock_code, m.list_date, "
        "COALESCE(c.is_active, 0) AS is_active, c.stock_name "
        "FROM stock_master AS m "
        "LEFT JOIN stock_current AS c ON c.stock_code=m.stock_code"
    )
    clauses: list[str] = []
    parameters: list[Any] = []
    if active_only:
        clauses.append("COALESCE(c.is_active, 0)=1")
    if stock_codes:
        placeholders = ",".join("?" for _ in stock_codes)
        clauses.append(f"m.stock_code IN ({placeholders})")
        parameters.extend(stock_codes)
    if clauses:
        sql = f"{sql} WHERE {' AND '.join(clauses)}"
    sql = f"{sql} ORDER BY m.stock_code"
    return [dict(row) for row in connection.execute(sql, parameters)]


def load_incremental_starts(
    connection: sqlite3.Connection,
    stock_code: str,
    fallback_date: str,
    end_date: str,
) -> dict[str, str]:
    # 优先使用同步状态；旧库首次执行时从各业务表最后记录日期初始化。
    states = {
        str(row["data_domain"]): str(row["last_success_date"])
        for row in connection.execute(
            "SELECT data_domain, last_success_date "
            "FROM stock_data_sync_state WHERE stock_code=?",
            (stock_code,),
        )
    }
    missing_domains = [
        domain for domain in DOMAIN_DATE_SOURCES if domain not in states
    ]
    if missing_domains:
        columns = []
        parameters: list[str] = []
        for domain in missing_domains:
            table, date_column = DOMAIN_DATE_SOURCES[domain]
            columns.append(
                f"(SELECT MAX({date_column}) FROM {table} "
                "WHERE stock_code=?)"
            )
            parameters.append(stock_code)
        latest = connection.execute(
            f"SELECT {', '.join(columns)}",
            parameters,
        ).fetchone()
        for domain, value in zip(missing_domains, latest, strict=True):
            if value:
                states[domain] = str(value)

    return {
        domain: min(states.get(domain, fallback_date), end_date)
        for domain in DOMAIN_DATE_SOURCES
    }


def save_sync_state(
    connection: sqlite3.Connection,
    stock_codes: Sequence[str],
    domains: Sequence[str],
    last_success_date: str,
) -> int:
    # 记录各股票、各数据域已经成功获取到的截止日期，空结果也视为成功。
    rows = [
        (stock_code, domain, last_success_date)
        for stock_code in stock_codes
        for domain in domains
    ]
    if not rows:
        return 0
    connection.executemany(
        "INSERT INTO stock_data_sync_state("
        "stock_code,data_domain,last_success_date,updated_at"
        ") VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%S','now','localtime')) "
        "ON CONFLICT(stock_code,data_domain) DO UPDATE SET "
        "last_success_date=MAX("
        "stock_data_sync_state.last_success_date,excluded.last_success_date"
        "), updated_at=excluded.updated_at",
        rows,
    )
    return len(rows)


def _insert_sql(
    table: str,
    columns: Sequence[str],
    conflict_columns: Sequence[str],
) -> str:
    # 生成所有非主键列均可更新的 SQLite UPSERT 语句。
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conflicts = ", ".join(conflict_columns)
    updates = ", ".join(
        f"{column}=excluded.{column}"
        for column in columns
        if column not in conflict_columns
    )
    return (
        f"INSERT INTO {table} ({names}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflicts}) DO UPDATE SET {updates}"
    )


def _upsert_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    conflict_columns: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> int:
    # 批量 UPSERT 指定表并返回输入记录数。
    row_list = list(rows)
    if not row_list:
        return 0
    sql = _insert_sql(table, columns, conflict_columns)
    connection.executemany(
        sql,
        (tuple(row.get(column) for column in columns) for row in row_list),
    )
    return len(row_list)


def upsert_daily_bars(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    # 批量保存股票日线行情。
    return _upsert_rows(
        connection,
        "stock_daily_bar",
        BAR_COLUMNS,
        ("stock_code", "trade_date"),
        rows,
    )


def upsert_capital_rows(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    # 批量保存股票每日股本。
    return _upsert_rows(
        connection,
        "stock_capital_daily",
        CAPITAL_COLUMNS,
        ("stock_code", "trade_date"),
        rows,
    )


def upsert_action_rows(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    # 批量保存股票公司行为。
    return _upsert_rows(
        connection,
        "stock_corporate_action",
        ACTION_COLUMNS,
        ("stock_code", "event_date", "action_type"),
        rows,
    )


def upsert_trade_rows(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    # 批量保存股票 GP 每日交易指标。
    return _upsert_rows(
        connection,
        "stock_trade_daily",
        TRADE_COLUMNS,
        ("stock_code", "trade_date"),
        rows,
    )


def upsert_financial_rows(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    # 批量保存财务报告；核心字段模式与既有完整 JSON 做增量合并。
    row_list = list(rows)
    if not row_list:
        return 0
    key_columns = ("stock_code", "report_date", "announce_date")
    names = ", ".join(FINANCIAL_COLUMNS)
    placeholders = ", ".join("?" for _ in FINANCIAL_COLUMNS)
    updates = []
    for column in FINANCIAL_COLUMNS:
        if column in key_columns:
            continue
        if column == "raw_values_json":
            updates.append(
                "raw_values_json=json_patch("
                "COALESCE(stock_financial_report.raw_values_json,'{}'),"
                "excluded.raw_values_json)"
            )
        elif column == "updated_at":
            updates.append("updated_at=excluded.updated_at")
        else:
            updates.append(
                f"{column}=COALESCE(excluded.{column},"
                f"stock_financial_report.{column})"
            )
    sql = (
        f"INSERT INTO stock_financial_report ({names}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET "
        f"{', '.join(updates)}"
    )
    connection.executemany(
        sql,
        (
            tuple(row.get(column) for column in FINANCIAL_COLUMNS)
            for row in row_list
        ),
    )
    return len(row_list)


def table_counts(
    connection: sqlite3.Connection,
    stock_codes: Sequence[str] | None = None,
) -> dict[str, int]:
    # 返回股票详细数据表的记录数量。
    counts: dict[str, int] = {}
    for table in DETAIL_TABLES:
        sql = f"SELECT COUNT(*) FROM {table}"
        parameters: list[str] = []
        if stock_codes:
            placeholders = ",".join("?" for _ in stock_codes)
            sql = f"{sql} WHERE stock_code IN ({placeholders})"
            parameters.extend(stock_codes)
        counts[table] = int(connection.execute(sql, parameters).fetchone()[0])
    return counts


def stock_table_counts(
    connection: sqlite3.Connection,
    stock_codes: Sequence[str],
) -> dict[str, dict[str, int]]:
    # 按股票和详细数据表统计记录数。
    result: dict[str, dict[str, int]] = {code: {} for code in stock_codes}
    for table in DETAIL_TABLES:
        placeholders = ",".join("?" for _ in stock_codes)
        sql = (
            f"SELECT stock_code, COUNT(*) AS row_count FROM {table} "
            f"WHERE stock_code IN ({placeholders}) GROUP BY stock_code"
        )
        values = {
            str(row["stock_code"]): int(row["row_count"])
            for row in connection.execute(sql, list(stock_codes))
        }
        for stock_code in stock_codes:
            result[stock_code][table] = values.get(stock_code, 0)
    return result


def daily_table_counts(
    connection: sqlite3.Connection,
    data_date: str,
) -> dict[str, int]:
    # 仅按日期索引统计当日记录，避免每日扫描全部历史数据。
    date_columns = {
        "stock_daily_bar": "trade_date",
        "stock_capital_daily": "trade_date",
        "stock_corporate_action": "event_date",
        "stock_trade_daily": "trade_date",
        "stock_financial_report": "announce_date",
    }
    return {
        table: int(connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {date_column}=?",
            (data_date,),
        ).fetchone()[0])
        for table, date_column in date_columns.items()
    }


def daily_database_checks(
    connection: sqlite3.Connection,
    data_date: str,
) -> dict[str, Any]:
    # 只检查当日新增数据与同步状态；相关查询均可使用日期或主键索引。
    bad_bars = int(connection.execute(
        "SELECT COUNT(*) FROM stock_daily_bar "
        "WHERE trade_date=? AND (high < low OR volume < 0 OR amount_10k < 0 "
        "OR open < low OR open > high OR close < low OR close > high)",
        (data_date,),
    ).fetchone()[0])
    bad_capital = int(connection.execute(
        "SELECT COUNT(*) FROM stock_capital_daily "
        "WHERE trade_date=? AND (total_shares < 0 OR float_shares < 0 "
        "OR float_shares > total_shares * 1.000001)",
        (data_date,),
    ).fetchone()[0])
    invalid_json = int(connection.execute(
        "SELECT COUNT(*) FROM stock_trade_daily "
        "WHERE trade_date=? AND json_valid(raw_metrics_json)=0",
        (data_date,),
    ).fetchone()[0])

    current_date = connection.execute(
        "SELECT MAX(data_date) FROM stock_current"
    ).fetchone()[0]
    expected_stocks = int(connection.execute(
        "SELECT COUNT(*) FROM stock_current "
        "WHERE data_date=? AND is_active=1 AND is_all_a=1",
        (data_date,),
    ).fetchone()[0])
    required_domains = ("bar", "capital", "action", "trade")
    completed_by_domain = {
        domain: int(connection.execute(
            "SELECT COUNT(*) FROM stock_current AS current "
            "JOIN stock_data_sync_state AS state "
            "ON state.stock_code=current.stock_code AND state.data_domain=? "
            "WHERE current.data_date=? AND current.is_active=1 "
            "AND current.is_all_a=1 AND state.last_success_date>=?",
            (domain, data_date, data_date),
        ).fetchone()[0])
        for domain in required_domains
    }
    incomplete_sync_rows = sum(
        max(expected_stocks - count, 0)
        for count in completed_by_domain.values()
    )
    return {
        "bad_bars": bad_bars,
        "bad_capital": bad_capital,
        "invalid_json": invalid_json,
        "stock_current_date": current_date,
        "expected_stocks": expected_stocks,
        "completed_by_domain": completed_by_domain,
        "incomplete_sync_rows": incomplete_sync_rows,
    }


def quick_database_check(connection: sqlite3.Connection) -> str:
    # 周度快速结构检查；仍会读取全库，但跳过完整索引一致性校验。
    return str(connection.execute("PRAGMA quick_check(1)").fetchone()[0])


def database_checks(connection: sqlite3.Connection) -> dict[str, Any]:
    # 执行 SQLite 完整性、外键和常用业务异常检查。
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    bad_bars = int(connection.execute(
        "SELECT COUNT(*) FROM stock_daily_bar "
        "WHERE high < low OR volume < 0 OR amount_10k < 0 "
        "OR open < low OR open > high OR close < low OR close > high"
    ).fetchone()[0])
    bad_capital = int(connection.execute(
        "SELECT COUNT(*) FROM stock_capital_daily "
        "WHERE total_shares < 0 OR float_shares < 0 "
        "OR float_shares > total_shares * 1.000001"
    ).fetchone()[0])
    invalid_json = int(connection.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM stock_trade_daily "
        "WHERE json_valid(raw_metrics_json)=0) + "
        "(SELECT COUNT(*) FROM stock_financial_report "
        "WHERE json_valid(raw_values_json)=0)"
    ).fetchone()[0])
    return {
        "integrity_check": integrity,
        "foreign_key_errors": foreign_key_errors,
        "bad_bars": bad_bars,
        "bad_capital": bad_capital,
        "invalid_json": invalid_json,
    }
