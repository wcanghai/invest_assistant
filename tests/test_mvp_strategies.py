"""典型量化策略 MVP 的核心计算测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_strategy.mvp import build_equal_weight_target, score_strategy


def _sample_data() -> dict:
    # 构造三只股票的行情、证券池和财务特征。
    codes = ["000001.SZ", "000002.SZ", "600000.SH"]
    universe = pd.DataFrame({
        "stock_name": ["甲", "乙", "丙"],
        "industry_code": ["I1", "I1", "I2"],
        "industry_name": ["行业一", "行业一", "行业二"],
        "close": [20.0, 15.0, 10.0],
        "adv_20": [1e8, 1e8, 1e8],
        "universe_flag": [1, 1, 1],
        "pe_ttm": [10.0, 20.0, 30.0],
        "pb_mrq": [1.0, 2.0, 3.0],
        "dividend_yield": [4.0, 2.0, 1.0],
    }, index=codes)
    financial = pd.DataFrame({
        "roe_pct": [20.0, 15.0, 10.0],
        "gross_margin_pct": [40.0, 30.0, 20.0],
        "debt_ratio_pct": [20.0, 40.0, 60.0],
        "revenue_yoy": [0.20, 0.10, 0.05],
        "parent_profit_yoy": [0.25, 0.10, 0.02],
    }, index=codes)
    dates = pd.bdate_range(end="2026-09-04", periods=260)
    bar_rows = []
    for number, code in enumerate(codes):
        growth = [1.002, 1.001, 1.0002][number]
        prices = 10.0 * growth ** np.arange(len(dates))
        for trade_date, price in zip(dates, prices, strict=True):
            bar_rows.append({
                "stock_code": code,
                "trade_date": trade_date,
                "adj_close": price,
                "return": growth - 1.0,
            })
    return {
        "universe": universe,
        "financial": financial,
        "bars": pd.DataFrame(bar_rows),
    }


def test_momentum_and_quality_value_rank_expected_stock_first() -> None:
    # 验证趋势和价值质量策略都能把优势股票排在首位。
    data = _sample_data()
    assert score_strategy(data, "momentum").index[0] == "000001.SZ"
    assert score_strategy(data, "quality_value").index[0] == "000001.SZ"


def test_equal_weight_target_limits_industry_count() -> None:
    # 验证目标组合实行行业数量上限并保持等权。
    scores = score_strategy(_sample_data(), "quality_value")
    target = build_equal_weight_target(scores, top_n=2, max_industry_fraction=0.50)
    assert len(target) == 2
    assert target["industry_code"].nunique() == 2
    assert np.allclose(target["target_weight"], 0.5)
