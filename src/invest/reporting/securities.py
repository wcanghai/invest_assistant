"""Read-only stock and ETF queries used by the local securities browser."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


MAX_BARS = 10000

FIELD_META = {
    "stock": {
        "基本信息": [("exchange", "交易所", ""), ("list_date", "上市日期", ""),
                    ("security_kind", "证券类型", ""), ("trade_unit", "交易单位", "股"),
                    ("min_price_tick", "最小价位", ""), ("price_precision", "价格精度", "位")],
        "市场属性": [("industry_name", "行业", ""), ("region_name", "地区", ""),
                    ("is_st", "ST", ""), ("is_suspended", "停牌", ""),
                    ("is_marginable", "融资融券", ""), ("is_stock_connect", "沪深股通", "")],
        "估值与规模": [("total_shares_10k", "总股本", "万股"), ("float_shares_10k", "流通股本", "万股"),
                      ("total_market_cap_100m", "总市值", "亿元"), ("float_market_cap_100m", "流通市值", "亿元"),
                      ("turnover_rate", "换手率", "%"), ("pe_ttm", "PE(TTM)", ""),
                      ("pb_mrq", "PB(MRQ)", ""), ("dividend_yield", "股息率", "%")],
        "财务信息": [("report_date", "报告期", ""), ("announce_date", "公告日期", ""),
                    ("basic_eps", "基本每股收益", "元"), ("roe_pct", "ROE", "%"),
                    ("revenue", "营业收入", ""), ("revenue_growth_pct", "营收增长", "%"),
                    ("net_profit", "净利润", ""), ("net_profit_growth_pct", "净利润增长", "%"),
                    ("gross_margin_pct", "毛利率", "%"), ("debt_ratio_pct", "资产负债率", "%")],
        "交易指标": [("shareholder_count", "股东户数", ""), ("financing_balance_10k", "融资余额", "万元"),
                    ("northbound_holding_shares", "北向持股", "股"), ("total_market_cap_10k", "交易指标市值", "万元"),
                    ("market_popularity_rank", "市场热度排名", ""), ("industry_popularity_rank", "行业热度排名", "")],
    },
    "etf": {
        "基本信息": [("exchange", "交易所", ""), ("list_date", "上市日期", ""),
                    ("underlying_code", "跟踪标的", ""), ("trade_unit", "交易单位", "份"),
                    ("is_t0", "T+0", ""), ("is_marginable", "融资融券", "")],
        "市场属性": [("latest_date", "最新数据日期", ""), ("etf_name", "基金名称", "")],
        "估值与规模": [("current_price", "最新价格", "元"), ("iopv", "IOPV", "元"),
                      ("size_100m", "基金规模", "亿元"), ("shares_10k", "份额", "万份"),
                      ("premium_discount_pct", "折溢价率", "%")],
        "交易指标": [("net_subscription_10k", "净申购", "万份"), ("pct_change", "涨跌幅", "%"),
                    ("volume", "成交量", "份"), ("amount_10k", "成交额", "万元")],
    },
}


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError("市场数据库不存在")
    conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    like = f"%{query}%"
    rows = conn.execute("""
        SELECT 'stock' AS security_type, m.stock_code AS code,
               COALESCE(c.stock_name, m.initial_name) AS name, m.exchange,
               COALESCE(c.data_date, m.updated_at) AS data_date
        FROM stock_master m LEFT JOIN stock_current c USING(stock_code)
        WHERE m.stock_code LIKE ? OR c.stock_name LIKE ? OR m.initial_name LIKE ?
        UNION ALL
        SELECT 'etf', e.etf_code, e.etf_name, e.exchange,
               COALESCE(d.trade_date, e.updated_at)
        FROM etf_master e LEFT JOIN (SELECT etf_code, MAX(trade_date) trade_date FROM etf_daily GROUP BY etf_code) d USING(etf_code)
        WHERE e.etf_code LIKE ? OR e.etf_name LIKE ?
        ORDER BY name, code LIMIT ?
    """, (like, like, like, like, like, limit)).fetchall()
    return [_dict(row) for row in rows]


def _security_kind(conn: sqlite3.Connection, code: str) -> str | None:
    if conn.execute("SELECT 1 FROM stock_master WHERE stock_code=?", (code,)).fetchone():
        return "stock"
    if conn.execute("SELECT 1 FROM etf_master WHERE etf_code=?", (code,)).fetchone():
        return "etf"
    return None


def detail(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    kind = _security_kind(conn, code)
    if kind == "stock":
        row = conn.execute("SELECT m.*, c.* FROM stock_master m LEFT JOIN stock_current c USING(stock_code) WHERE m.stock_code=?", (code,)).fetchone()
        financial = conn.execute("SELECT * FROM stock_financial_report WHERE stock_code=? ORDER BY announce_date DESC, report_date DESC LIMIT 1", (code,)).fetchone()
        trade = conn.execute("SELECT * FROM stock_trade_daily WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
        payload = _dict(row) or {}
        if financial:
            payload.update({k: v for k, v in _dict(financial).items() if k not in {"stock_code", "updated_at"}})
        if trade:
            payload.update({k: v for k, v in _dict(trade).items() if k not in {"stock_code", "raw_metrics_json", "updated_at"}})
        name = payload.get("stock_name") or payload.get("initial_name") or code
    elif kind == "etf":
        row = conn.execute("SELECT e.*, d.* FROM etf_master e LEFT JOIN etf_daily d ON d.etf_code=e.etf_code AND d.trade_date=(SELECT MAX(trade_date) FROM etf_daily WHERE etf_code=e.etf_code) WHERE e.etf_code=?", (code,)).fetchone()
        payload = _dict(row) or {}
        name = payload.get("etf_name") or code
    else:
        return None
    payload.pop("raw_info_json", None)
    payload["security_type"] = kind
    payload["code"] = code
    payload["name"] = name
    payload["field_meta"] = FIELD_META[kind]
    return payload


def _period_key(value: str, period: str) -> str:
    current = datetime.strptime(value, "%Y-%m-%d").date()
    if period == "day":
        return value
    if period == "week":
        monday = current - timedelta(days=current.weekday())
        return monday.isoformat()
    if period == "month":
        return current.strftime("%Y-%m")
    if period == "quarter":
        return f"{current.year}-Q{(current.month - 1) // 3 + 1}"
    return str(current.year)


def _aggregate(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    if period == "day":
        return rows
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_period_key(row["trade_date"], period), []).append(row)
    result = []
    for key, group in grouped.items():
        first, last = group[0], group[-1]
        item = dict(last)
        item.update({"trade_date": key, "open": first["open"],
                     "high": max((x["high"] for x in group if x["high"] is not None), default=None),
                     "low": min((x["low"] for x in group if x["low"] is not None), default=None),
                     "volume": sum(x["volume"] or 0 for x in group),
                     "amount_10k": sum(x["amount_10k"] or 0 for x in group)})
        base = first.get("pre_close") or first.get("open")
        item["pct_change"] = ((last["close"] / base) - 1) * 100 if base and last.get("close") is not None else None
        result.append(item)
    return result


def bars(conn: sqlite3.Connection, code: str, start: str | None = None,
         end: str | None = None, period: str = "day") -> dict[str, Any]:
    kind = _security_kind(conn, code)
    if not kind:
        raise ValueError("证券不存在")
    try:
        end_date = date.fromisoformat(end) if end else date.today()
        start_date = date.fromisoformat(start) if start else end_date - timedelta(days=365)
    except ValueError as exc:
        raise ValueError("日期格式必须为 YYYY-MM-DD") from exc
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    if period not in {"day", "week", "month", "quarter", "year"}:
        raise ValueError("不支持的 K 线周期")
    table = "stock_daily_bar" if kind == "stock" else "etf_daily"
    columns = ("trade_date, open, high, low, close, volume, amount_10k, pct_change, "
               "is_trading, pre_close" if kind == "stock" else
               "trade_date, open, high, low, close, volume, amount_10k, pct_change, "
               "is_trading, pre_close, unit_nav, cumulative_nav, premium_discount_pct, "
               "net_subscription_10k")
    key = "stock_code" if kind == "stock" else "etf_code"
    rows = conn.execute(f"SELECT {columns} FROM {table} WHERE {key}=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date LIMIT ?", (code, start_date.isoformat(), end_date.isoformat(), MAX_BARS + 1)).fetchall()
    raw_rows = [_dict(row) for row in rows]
    if len(raw_rows) > MAX_BARS:
        raise ValueError(f"查询结果超过 {MAX_BARS} 条，请缩小时间范围")
    return {"security_type": kind, "code": code, "period": period,
            "start": start_date.isoformat(), "end": end_date.isoformat(),
            "bars": _aggregate(raw_rows, period)}
