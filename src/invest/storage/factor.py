"""多因子策略的 SQLite 查询和结果写入。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from invest.storage.stock import connect as stock_connect


def connect(
    db_path: str | Path,
    market_db: str | Path | None = None,
) -> sqlite3.Connection:
    # 打开研究库，并在拆分模式下用只读临时视图提供市场数据。
    if market_db is None:
        return stock_connect(db_path)
    market_path = Path(market_db).resolve()
    if not market_path.is_file() or not market_path.stat().st_size:
        raise FileNotFoundError(f"市场数据库不存在或为空: {market_path}")
    result_path = Path(db_path).resolve()
    if result_path == market_path:
        raise ValueError("市场库和研究结果库必须分离")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(result_path.as_uri(), uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    tables = (
        "stock_master", "stock_current", "stock_daily_bar", "stock_capital_daily",
        "stock_financial_report", "stock_trade_daily", "stock_sector_snapshot",
        "stock_snapshot", "stock_corporate_action",
    )
    try:
        connection.execute(
            "ATTACH DATABASE ? AS market_source", (market_path.as_uri() + "?mode=ro",)
        )
        for table in tables:
            connection.execute(
                f'CREATE TEMP VIEW "{table}" AS SELECT * FROM market_source."{table}"'
            )
    except Exception:
        connection.close()
        raise
    return connection


def init_strategy_schema(connection: sqlite3.Connection) -> None:
    # 创建简化版策略所需的五张结果表。
    schema_path = Path(__file__).with_name("factor.sql")
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    connection.commit()


def _read_frame(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> pd.DataFrame:
    # 执行参数化查询并返回 DataFrame。
    return pd.read_sql_query(sql, connection, params=tuple(parameters))


def _code_clause(codes: Sequence[str]) -> tuple[str, list[str]]:
    # 为股票代码列表生成安全的 SQL 占位符。
    if not codes:
        return "(NULL)", []
    return f"({','.join('?' for _ in codes)})", list(codes)


def load_snapshot(
    connection: sqlite3.Connection,
    factor_date: str,
) -> pd.DataFrame:
    # 读取指定日期的历史证券状态和上市日期。
    return _read_frame(
        connection,
        "SELECT s.*,m.list_date,m.exchange,m.security_kind "
        "FROM stock_snapshot AS s JOIN stock_master AS m USING(stock_code) "
        "WHERE s.snapshot_date=?",
        (factor_date,),
    )


def load_trade_dates(
    connection: sqlite3.Connection,
    end_date: str,
    limit: int | None = None,
) -> list[str]:
    # 从日线表读取不晚于指定日期的真实交易日。
    sql = "SELECT DISTINCT trade_date FROM stock_daily_bar WHERE trade_date<=? "
    parameters: list[Any] = [end_date]
    sql += "ORDER BY trade_date DESC"
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    rows = connection.execute(sql, parameters).fetchall()
    return sorted(str(row[0]) for row in rows)


def next_trade_date(
    connection: sqlite3.Connection,
    factor_date: str,
) -> str | None:
    # 返回指定日期后的首个数据库交易日。
    row = connection.execute(
        "SELECT MIN(trade_date) FROM stock_daily_bar WHERE trade_date>?",
        (factor_date,),
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def load_market_day(
    connection: sqlite3.Connection,
    trade_date: str,
) -> pd.DataFrame:
    # 读取一个交易日全部股票的行情和涨跌停状态。
    return _read_frame(
        connection,
        "SELECT b.*,t.limit_status FROM stock_daily_bar AS b "
        "LEFT JOIN stock_trade_daily AS t "
        "ON t.stock_code=b.stock_code AND t.trade_date=b.trade_date "
        "WHERE b.trade_date=?",
        (trade_date,),
    )


def load_bars(
    connection: sqlite3.Connection,
    codes: Sequence[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    # 读取股票列表在日期窗口内的日线。
    clause, values = _code_clause(codes)
    sql = (
        "SELECT * FROM stock_daily_bar WHERE stock_code IN "
        f"{clause} AND trade_date BETWEEN ? AND ? ORDER BY stock_code,trade_date"
    )
    return _read_frame(connection, sql, [*values, start_date, end_date])


def load_capital(
    connection: sqlite3.Connection,
    codes: Sequence[str],
    end_date: str,
) -> pd.DataFrame:
    # 读取因子日前全部必要股本记录。
    clause, values = _code_clause(codes)
    sql = (
        "SELECT * FROM stock_capital_daily WHERE stock_code IN "
        f"{clause} AND trade_date<=? ORDER BY stock_code,trade_date"
    )
    return _read_frame(connection, sql, [*values, end_date])


def load_actions(
    connection: sqlite3.Connection,
    codes: Sequence[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    # 读取现金分红和其他公司行为。
    clause, values = _code_clause(codes)
    sql = (
        "SELECT * FROM stock_corporate_action WHERE stock_code IN "
        f"{clause} AND event_date BETWEEN ? AND ? ORDER BY stock_code,event_date"
    )
    return _read_frame(connection, sql, [*values, start_date, end_date])


def load_trade_metrics(
    connection: sqlite3.Connection,
    codes: Sequence[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    # 读取因子窗口内的股票交易指标。
    clause, values = _code_clause(codes)
    sql = (
        "SELECT * FROM stock_trade_daily WHERE stock_code IN "
        f"{clause} AND trade_date BETWEEN ? AND ? ORDER BY stock_code,trade_date"
    )
    return _read_frame(connection, sql, [*values, start_date, end_date])


def load_financials_asof(
    connection: sqlite3.Connection,
    codes: Sequence[str],
    factor_date: str,
) -> pd.DataFrame:
    # 读取因子日前已公告的全部财务版本供时点计算。
    clause, values = _code_clause(codes)
    sql = (
        "SELECT * FROM stock_financial_report WHERE stock_code IN "
        f"{clause} AND announce_date<=? ORDER BY stock_code,report_date,announce_date"
    )
    return _read_frame(connection, sql, [*values, factor_date])


def upsert_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: Iterable[dict[str, Any]],
    conflict_columns: Sequence[str],
) -> int:
    # 批量 UPSERT 同一结构的策略结果行。
    row_list = list(rows)
    if not row_list:
        return 0
    columns = tuple(row_list[0])
    names = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    conflicts = ",".join(conflict_columns)
    updates = ",".join(
        f"{column}=excluded.{column}"
        for column in columns
        if column not in conflict_columns
    )
    sql = (
        f"INSERT INTO {table}({names}) VALUES({placeholders}) "
        f"ON CONFLICT({conflicts}) DO UPDATE SET {updates}"
    )
    connection.executemany(sql, [tuple(row[column] for column in columns) for row in row_list])
    connection.commit()
    return len(row_list)
