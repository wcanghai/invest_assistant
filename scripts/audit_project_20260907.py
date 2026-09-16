"""只读审计数据库，保存 SQL、结果和查询耗时，便于复核工程评估。"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "doc" / "project_audit_20260907_evidence.json"


def main() -> None:
    # 在只读事务中核对覆盖率和数据质量，并输出可复查的证据文件。
    database = ROOT / "data" / "security_pool.db"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    report = {
        "started_at": datetime.now().astimezone().isoformat(),
        "database": str(database),
        "main_file_bytes": database.stat().st_size,
        "wal_file_bytes": Path(str(database) + "-wal").stat().st_size,
        "mode": "SQLite mode=ro; query_only=ON; single read transaction",
        "queries": [],
    }
    queries = [
        ("tables", "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"),
        ("snapshots", "SELECT snapshot_date,COUNT(*) n FROM stock_snapshot "
         "GROUP BY snapshot_date"),
        ("universe", "SELECT COUNT(*) n,SUM(is_active) active,SUM(is_tradable) tradable,"
         "MIN(data_date) min_date,MAX(data_date) max_date FROM stock_current"),
        ("stock_sync", "SELECT data_domain,MIN(last_success_date) min_date,"
         "MAX(last_success_date) max_date,COUNT(*) n FROM stock_data_sync_state "
         "GROUP BY data_domain"),
        ("etf_sync", "SELECT data_domain,MIN(last_success_date) min_date,"
         "MAX(last_success_date) max_date,COUNT(*) n FROM etf_data_sync_state "
         "GROUP BY data_domain"),
        ("task_status", "SELECT status,COUNT(*) n FROM fetch_log GROUP BY status"),
        ("unfinished_tasks", "SELECT * FROM fetch_log WHERE status='RUNNING'"),
        ("latest_tasks", "SELECT * FROM fetch_log ORDER BY id DESC LIMIT 8"),
        ("financial_quality", "SELECT COUNT(*) n,COUNT(DISTINCT stock_code) securities,"
         "MIN(report_date) min_report,MAX(report_date) max_report,"
         "MIN(announce_date) min_announce,MAX(announce_date) max_announce,"
         "SUM(announce_date<report_date) announce_before_report,"
         "SUM(announce_date='') empty_announce,SUM(revenue IS NULL) revenue_null,"
         "SUM(revenue=0) revenue_zero,SUM(parent_net_profit IS NULL) parent_profit_null,"
         "SUM(operating_cash_flow IS NULL) ocf_null,"
         "SUM(operating_cash_flow=0) ocf_zero FROM stock_financial_report"),
        ("financial_latest_distribution", "SELECT last_report,COUNT(*) n FROM "
         "(SELECT stock_code,MAX(report_date) last_report FROM stock_financial_report "
         "GROUP BY stock_code) GROUP BY last_report ORDER BY last_report DESC"),
        ("financial_missing", "SELECT m.stock_code,c.stock_name,m.list_date,c.is_tradable "
         "FROM stock_master m JOIN stock_current c USING(stock_code) "
         "WHERE NOT EXISTS (SELECT 1 FROM stock_financial_report f "
         "WHERE f.stock_code=m.stock_code)"),
        ("missing_listing", "SELECT m.stock_code,c.stock_name,m.list_date,c.is_tradable "
         "FROM stock_master m JOIN stock_current c USING(stock_code) "
         "WHERE m.list_date IS NULL OR m.list_date=''"),
        ("latest_bar_quality", "SELECT COUNT(*) n,SUM(close>0) positive_close,"
         "SUM(is_trading=1) trading,SUM(forward_factor IS NULL) missing_factor,"
         "SUM(pre_close IS NULL) missing_preclose,SUM(pct_change IS NULL) missing_return,"
         "SUM(close>high OR close<low OR open>high OR open<low) bad_ohlc "
         "FROM stock_daily_bar WHERE trade_date=(SELECT MAX(trade_date) "
         "FROM stock_daily_bar)"),
        ("latest_trade_coverage", "SELECT COUNT(*) n,MAX(trade_date) trade_date,"
         "COUNT(total_market_cap_10k) market_cap,COUNT(dividend_yield_pct) dividend,"
         "COUNT(northbound_holding_shares) northbound,COUNT(financing_balance_10k) margin,"
         "COUNT(pledge_ratio_pct) pledge,COUNT(market_popularity_rank) popularity,"
         "COUNT(limit_status) limit_status FROM stock_trade_daily "
         "WHERE trade_date=(SELECT MAX(trade_date) FROM stock_trade_daily)"),
        ("latest_etf_coverage", "SELECT COUNT(*) n,MAX(trade_date) trade_date,"
         "COUNT(close) close_n,COUNT(unit_nav) nav_n,COUNT(shares_10k) shares_n,"
         "COUNT(net_subscription_10k) subscription_n FROM etf_daily "
         "WHERE trade_date=(SELECT MAX(trade_date) FROM etf_daily)"),
        ("etf_master_quality", "SELECT COUNT(*) n,SUM(list_date IS NULL OR list_date='') "
         "missing_listing,SUM(underlying_code IS NULL OR underlying_code='') "
         "missing_underlying,SUM(is_t0) t0 FROM etf_master"),
        ("etf_field_latest", "SELECT MAX(CASE WHEN close IS NOT NULL THEN trade_date END) "
         "close_date,MAX(CASE WHEN unit_nav IS NOT NULL THEN trade_date END) nav_date,"
         "MAX(CASE WHEN shares_10k IS NOT NULL THEN trade_date END) shares_date,"
         "COUNT(net_subscription_10k) subscription_n FROM etf_daily"),
    ]
    tables = {
        "stock_master": ("stock_code", "first_seen_date"),
        "stock_sector_snapshot": ("stock_code", "snapshot_date"),
        "stock_daily_bar": ("stock_code", "trade_date"),
        "stock_capital_daily": ("stock_code", "trade_date"),
        "stock_corporate_action": ("stock_code", "event_date"),
        "stock_trade_daily": ("stock_code", "trade_date"),
        "etf_daily": ("etf_code", "trade_date"),
        "etf_market_daily": (None, "trade_date"),
        "etf_snapshot": ("etf_code", "snapshot_date"),
        "strategy_run": (None, None),
        "factor_score_daily": ("stock_code", "factor_date"),
        "portfolio_target": ("stock_code", "rebalance_date"),
        "backtest_nav": (None, "trade_date"),
        "backtest_trade": ("stock_code", "trade_date"),
    }
    for table, (code, date) in tables.items():
        columns = ["COUNT(*) n"]
        if code:
            columns.append(f"COUNT(DISTINCT {code}) securities")
        if date:
            columns.extend([f"MIN({date}) min_date", f"MAX({date}) max_date"])
        queries.append((table, f"SELECT {','.join(columns)} FROM {table}"))
    for name, sql in queries:
        started = time.monotonic()
        item = {"name": name, "sql": sql}
        try:
            item["rows"] = [dict(row) for row in connection.execute(sql)]
        except sqlite3.Error as error:
            item["error"] = str(error)
        item["seconds"] = round(time.monotonic() - started, 3)
        report["queries"].append(item)
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(item, ensure_ascii=False), flush=True)
    connection.rollback()
    connection.close()
    report["finished_at"] = datetime.now().astimezone().isoformat()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
