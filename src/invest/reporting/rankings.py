"""Read-only stock ranking queries for the local rankings page."""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any


METRICS = {
    "day_return": ("当日涨跌幅", "%", "market"),
    "volume": ("当日成交量", "股", "market"),
    "month_return": ("20日涨跌幅", "%", "market"),
    "year_return": ("250日涨跌幅", "%", "market"),
    "revenue": ("营业收入", "元", "financial"),
    "net_profit": ("净利润", "元", "financial"),
    "net_margin": ("净利润率", "%", "financial"),
    "roe": ("ROE", "%", "financial"),
    "amount": ("成交额", "万元", "market"),
    "turnover": ("换手率", "%", "market"),
    "market_cap": ("总市值", "亿元", "market"),
    "dividend": ("股息率", "%", "market"),
    "pe": ("PE", "", "market"),
    "pb": ("PB", "", "market"),
    "revenue_growth": ("营收增长率", "%", "financial"),
    "profit_growth": ("净利润增长率", "%", "financial"),
    "gross_margin": ("毛利率", "%", "financial"),
    "debt_ratio": ("资产负债率", "%", "financial"),
}

_CACHE: dict[tuple[str, str, tuple[tuple[str, str], ...]], tuple[float, dict[str, Any]]] = {}


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    # 以只读方式连接市场库。
    """以只读方式连接市场库。"""
    target = Path(path).resolve()
    conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _universe(filters: dict[str, str]) -> tuple[str, list[Any]]:
    # 构造固定字段的股票池过滤条件。
    """构造固定字段的股票池过滤条件。"""
    where = ["c.is_active=1", "COALESCE(c.is_all_a, 0)=1"]
    args: list[Any] = []
    if filters.get("include_st") != "1":
        where.append("COALESCE(c.is_st, 0)=0")
    if filters.get("include_suspended") != "1":
        where.append("COALESCE(c.is_suspended, 0)=0")
    board = filters.get("board", "")
    board_map = {"main": "is_main_board", "gem": "is_gem", "star": "is_star", "bj": "is_bj_a"}
    if board in board_map:
        where.append(f"COALESCE(c.{board_map[board]}, 0)=1")
    if filters.get("industry"):
        where.append("c.industry_name=?")
        args.append(filters["industry"])
    return " AND ".join(where), args


def meta(conn: sqlite3.Connection) -> dict[str, Any]:
    # 返回页面筛选项与最新数据日期。
    """返回页面筛选项与最新数据日期。"""
    latest = conn.execute("SELECT MAX(trade_date) FROM stock_daily_bar").fetchone()[0]
    reports = [r[0] for r in conn.execute(
        "SELECT DISTINCT report_date FROM stock_financial_report ORDER BY report_date DESC"
    ).fetchall()]
    industries = [r[0] for r in conn.execute(
        "SELECT DISTINCT industry_name FROM stock_current "
        "WHERE industry_name IS NOT NULL ORDER BY industry_name"
    ).fetchall()]
    return {"latest_date": latest, "report_dates": reports, "industries": industries,
            "boards": {"": "全部板块", "main": "主板", "gem": "创业板",
                       "star": "科创板", "bj": "北交所"},
            "metrics": {k: {"title": v[0], "unit": v[1], "group": v[2]}
                        for k, v in METRICS.items()}}


def query(conn: sqlite3.Connection, metric: str, filters: dict[str, str]) -> dict[str, Any]:
    # 查询单项指标的高低排行榜。
    """查询单项指标的高低排行榜。"""
    if metric not in METRICS:
        raise ValueError("不支持的排行榜指标")
    db_name = conn.execute("PRAGMA database_list").fetchone()[2]
    cache_key = (db_name, metric, tuple(sorted(filters.items())))
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    where, args = _universe(filters)
    latest = conn.execute("SELECT MAX(trade_date) FROM stock_daily_bar").fetchone()[0]
    group = METRICS[metric][2]
    if group == "financial":
        report_date = filters.get("report_date") or conn.execute(
            "SELECT MAX(report_date) FROM stock_financial_report WHERE announce_date<=?",
            (date.today().isoformat(),),
        ).fetchone()[0]
        expr = {"revenue": "f.revenue", "net_profit": "f.net_profit",
                "net_margin": "100.0*f.net_profit/NULLIF(f.revenue,0)",
                "roe": "f.roe_pct", "revenue_growth": "f.revenue_growth_pct",
                "profit_growth": "f.net_profit_growth_pct",
                "gross_margin": "f.gross_margin_pct", "debt_ratio": "f.debt_ratio_pct"}[metric]
        source = """FROM stock_current c JOIN stock_master m USING(stock_code)
        JOIN (SELECT * FROM stock_financial_report WHERE report_date=? AND announce_date<=?
              AND rowid IN (SELECT MAX(rowid) FROM stock_financial_report
              WHERE report_date=? AND announce_date<=? GROUP BY stock_code)) f USING(stock_code)"""
        args = [report_date, date.today().isoformat(), report_date, date.today().isoformat(), *args]
        data_date = report_date
    else:
        expr_map = {"day_return": "b.pct_change", "volume": "b.volume",
                    "amount": "b.amount_10k", "turnover": "c.turnover_rate",
                    "market_cap": "c.total_market_cap_100m", "dividend": "c.dividend_yield",
                    "pe": "c.pe_ttm", "pb": "c.pb_mrq"}
        if metric in expr_map:
            expr = expr_map[metric]
            source = ("FROM stock_current c JOIN stock_master m USING(stock_code) JOIN "
                      "stock_daily_bar b ON b.stock_code=c.stock_code AND b.trade_date=?")
            args = [latest, *args]
        else:
            periods = 20 if metric == "month_return" else 250
            expr = f"100.0*(latest.adj_close/old.adj_close-1)"
            source = f"""FROM stock_current c JOIN stock_master m USING(stock_code)
            JOIN (SELECT stock_code, close*COALESCE(forward_factor,1) adj_close FROM stock_daily_bar
                  WHERE trade_date=? ) latest USING(stock_code)
            JOIN (SELECT stock_code, close*COALESCE(forward_factor,1) adj_close FROM
                  (SELECT stock_code, close, forward_factor, ROW_NUMBER() OVER
                   (PARTITION BY stock_code ORDER BY trade_date DESC) rn FROM stock_daily_bar
                   WHERE trade_date<=?) WHERE rn=?) old USING(stock_code)"""
            args = [latest, latest, periods + 1, *args]
            data_date = latest
        if metric in {"pe", "pb"}:
            where += f" AND {expr}>0"
        if metric in {"day_return", "volume", "amount"}:
            where += f" AND {expr} IS NOT NULL"
        if metric in {"month_return", "year_return"}:
            where += " AND latest.adj_close IS NOT NULL AND old.adj_close>0"
        data_date = latest
    sql = (f"SELECT c.stock_code code, COALESCE(c.stock_name,m.initial_name) name, "
           f"c.industry_name, {expr} value, ? data_date {source} WHERE {where} "
           f"AND ({expr}) IS NOT NULL ORDER BY value DESC, c.stock_code")
    rows = [dict(r) for r in conn.execute(sql, [data_date, *args]).fetchall()]
    low = sorted(rows, key=lambda r: (r["value"], r["code"]))[:100]
    for values in (rows, low):
        for i, row in enumerate(values, 1):
            row["rank"] = i
    result = {"metric": metric, "title": METRICS[metric][0], "unit": METRICS[metric][1],
              "data_date": data_date, "sample_count": len(rows), "high": rows[:100],
              "low": low}
    _CACHE[cache_key] = (time.time(), result)
    return result
