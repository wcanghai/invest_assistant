"""简化版多因子回测的端到端测试。"""

from __future__ import annotations

from invest.research.stocks.backtest import run_backtest
from invest.research.stocks.config import load_config

from tests.test_factor_strategy import _seed_database


def test_monthly_backtest_runs_and_balances_nav() -> None:
    # 验证月度回测可运行且每日净值等于现金加持仓市值。
    connection = _seed_database()
    try:
        config = load_config()
        config["portfolio"]["target_count"] = 5
        config["portfolio"]["buy_rank_pct"] = 0.80
        config["portfolio"]["hold_rank_pct"] = 0.90
        config["portfolio"]["max_stock_weight"] = 0.30
        config["portfolio"]["max_industry_weight"] = 0.60
        result = run_backtest(
            connection,
            "2026-06-01",
            "2026-08-31",
            config,
        )
        nav = result["nav"]
        assert not nav.empty
        assert not result["trades"].empty
        assert (nav["nav"] > 0).all()
        difference = nav["nav"] - nav["cash"] - nav["holding_value"]
        assert difference.abs().max() < 1e-6
    finally:
        connection.close()
