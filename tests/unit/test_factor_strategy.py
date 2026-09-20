"""简化版多因子策略的核心计算和集成测试。"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd

from invest.storage import factor as db
from invest.research.stocks.config import load_config
from invest.research.stocks.data import build_financial_features
from invest.research.stocks.data import load_factor_data
from invest.research.stocks.portfolio import build_portfolio
from invest.research.stocks.preprocess import rank_normalize
from invest.research.stocks.preprocess import winsorize_mad
from invest.research.stocks.scoring import calculate_scores
from invest.storage import stock as stock_db


def _financial_row(
    stock_code: str,
    report_date: str,
    announce_date: str,
    revenue: float,
    profit: float,
) -> dict:
    # 构造一条字段完整的测试财务报告。
    return {
        "stock_code": stock_code,
        "report_date": report_date,
        "announce_date": announce_date,
        "basic_eps": 1.0,
        "deduct_eps": 0.9,
        "book_value_per_share": 8.0,
        "roe_pct": 12.0,
        "total_assets": 10_000.0,
        "total_liabilities": 4_000.0,
        "total_equity": 6_000.0,
        "operating_cash_flow": profit * 1.1,
        "net_profit": profit,
        "revenue": revenue,
        "operating_profit": profit * 1.2,
        "parent_net_profit": profit,
        "deduct_net_profit": profit * 0.9,
        "revenue_growth_pct": 10.0,
        "net_profit_growth_pct": 12.0,
        "gross_margin_pct": 35.0,
        "debt_ratio_pct": 40.0,
        "total_shares": 1_000.0,
        "float_a_shares": 800.0,
        "shareholder_count": 10_000.0,
        "raw_values_json": "{}",
        "updated_at": "2026-08-31T18:00:00",
    }


def _insert_dict(connection, table: str, row: dict) -> None:
    # 将字典按键顺序插入测试数据库。
    columns = tuple(row)
    sql = (
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})"
    )
    connection.execute(sql, tuple(row[column] for column in columns))


def _seed_database() -> sqlite3.Connection:
    # 创建包含十二只股票和三百个交易日的内存数据库。
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    stock_db.init_db(connection)
    db.init_strategy_schema(connection)
    dates = pd.bdate_range(end="2026-08-31", periods=300)
    snapshot_dates = {"2026-06-30", "2026-07-31", "2026-08-31"}
    for number in range(12):
        code = f"600{number:03d}.SH"
        _insert_dict(connection, "stock_master", {
            "stock_code": code,
            "exchange": "SH",
            "security_kind": 1,
            "list_date": "2020-01-01",
            "initial_name": f"测试{number}",
            "trade_unit": 100.0,
            "min_price_tick": 0.01,
            "price_precision": 2,
            "first_seen_date": "2020-01-01",
            "created_at": "2020-01-01T00:00:00",
            "updated_at": "2026-08-31T18:00:00",
        })
        for snapshot_date in snapshot_dates:
            connection.execute(
                "INSERT INTO stock_snapshot("
                "snapshot_date,stock_code,stock_name,is_active,is_all_a,is_st,"
                "is_delisting_board,is_suspended,industry_code,industry_name,"
                "total_shares_10k,float_shares_10k,is_tradable,archived_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_date,
                    code,
                    f"测试{number}",
                    1,
                    1,
                    0,
                    0,
                    0,
                    f"I{number % 3}",
                    f"行业{number % 3}",
                    10_000.0,
                    8_000.0,
                    1,
                    f"{snapshot_date}T18:00:00",
                ),
            )
        connection.execute(
            "INSERT INTO stock_capital_daily("
            "stock_code,trade_date,total_shares,float_shares,updated_at) "
            "VALUES(?,?,?,?,?)",
            (code, "2020-01-01", 100_000_000.0, 80_000_000.0, "2026-08-31"),
        )
        base = 8.0 + number
        for index, trade_day in enumerate(dates):
            close = base * (1.0 + 0.0008 * (number + 1)) ** index
            amount_10k = 3000.0 + number * 500.0
            connection.execute(
                "INSERT INTO stock_daily_bar("
                "stock_code,trade_date,open,high,low,close,volume,amount_10k,"
                "forward_factor,pre_close,pct_change,is_trading,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    code,
                    trade_day.strftime("%Y-%m-%d"),
                    close * 0.999,
                    close * 1.01,
                    close * 0.99,
                    close,
                    1_000_000.0 + number * 10_000,
                    amount_10k,
                    1.0,
                    None,
                    None,
                    1,
                    "2026-08-31T18:00:00",
                ),
            )
        for trade_day in dates[-25:]:
            connection.execute(
                "INSERT INTO stock_trade_daily("
                "stock_code,trade_date,shareholder_count,financing_balance_10k,"
                "northbound_holding_shares,financing_net_buy_10k,total_market_cap_10k,"
                "dividend_yield_pct,limit_status,limit_order_amount_10k,"
                "pledge_ratio_pct,market_popularity_rank,industry_popularity_rank,"
                "raw_metrics_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    code,
                    trade_day.strftime("%Y-%m-%d"),
                    10_000 - number * 10,
                    1_000 + number,
                    1_000_000 + number * 10_000,
                    10 + number,
                    100_000 + number * 1_000,
                    2.0 + number * 0.1,
                    0,
                    20 + number,
                    5.0 + number,
                    100 - number,
                    20 - number / 2,
                    "{}",
                    "2026-08-31T18:00:00",
                ),
            )
        for report in (
            _financial_row(code, "2022-06-30", "2022-08-20", 500.0, 50.0),
            _financial_row(code, "2024-06-30", "2024-08-20", 700.0, 70.0),
            _financial_row(code, "2024-12-31", "2025-03-30", 1500.0, 150.0),
            _financial_row(
                code,
                "2025-06-30",
                "2025-08-20",
                800.0 + number * 10,
                80.0 + number,
            ),
            _financial_row(code, "2026-06-30", "2026-09-20", 900.0, 90.0),
        ):
            _insert_dict(connection, "stock_financial_report", report)
    connection.commit()
    return connection


def test_ttm_uses_only_available_financial_versions() -> None:
    # 验证 TTM 公式并确认未来公告不会进入因子日截面。
    connection = _seed_database()
    try:
        raw = db.load_financials_asof(connection, ["600000.SH"], "2026-08-31")
        features = build_financial_features(raw)
        assert features.at["600000.SH", "report_date"] == "2025-06-30"
        assert features.at["600000.SH", "revenue_ttm"] == 1600.0
        assert features.at["600000.SH", "parent_net_profit_ttm"] == 160.0
    finally:
        connection.close()


def test_preprocessing_handles_outlier_and_ranks() -> None:
    # 验证 MAD 缩尾和秩标准分保持顺序且限制极端值。
    source = pd.Series([1.0, 2.0, 3.0, 4.0, 1000.0])
    clipped = winsorize_mad(source, 3.0)
    ranked = rank_normalize(clipped)
    assert clipped.iloc[-1] < 1000.0
    assert ranked.is_monotonic_increasing
    assert ranked.max() == 1.0


def test_end_to_end_scores_and_portfolio() -> None:
    # 验证内存数据库可以完成数据读取、七类评分和组合构建。
    connection = _seed_database()
    try:
        config = load_config()
        config["portfolio"]["target_count"] = 5
        config["portfolio"]["buy_rank_pct"] = 0.80
        config["portfolio"]["hold_rank_pct"] = 0.90
        config["portfolio"]["max_stock_weight"] = 0.30
        config["portfolio"]["max_industry_weight"] = 0.60
        data = load_factor_data(connection, "2026-08-31", config)
        scores = calculate_scores(data, config)
        target = build_portfolio(scores, None, config)
        assert data["universe"]["universe_flag"].sum() >= 8
        assert scores["total_score"].notna().sum() >= 8
        assert len(target) == 5
        assert target["target_weight"].max() <= 0.30 + 1e-9
        assert 0.99 <= target["target_weight"].sum() <= 1.0 + 1e-9
        assert json.loads(scores.iloc[0]["factor_detail_json"])
    finally:
        connection.close()
