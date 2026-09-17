"""财务质量因子模型。"""

from __future__ import annotations

import numpy as np
import pandas as pd


WEIGHTS = {
    "roe_ttm": 0.25,
    "ocf_to_assets": 0.20,
    "accrual_quality": 0.20,
    "gross_margin": 0.15,
    "low_leverage": 0.10,
    "profit_stability": 0.10,
}


def calculate(data: dict, config: dict) -> pd.DataFrame:
    # 计算盈利能力、现金质量、杠杆和稳定性因子。
    universe = data["universe"]
    financial = data["financial"].reindex(universe.index)
    result = pd.DataFrame(index=universe.index)
    average_equity = financial["equity_average"].where(
        financial["equity_average"] > 0
    )
    average_assets = financial["assets_average"].where(
        financial["assets_average"] > 0
    )
    result["roe_ttm"] = financial["parent_net_profit_ttm"] / average_equity
    result["ocf_to_assets"] = financial["operating_cash_flow_ttm"] / average_assets
    result["accrual_quality"] = -(
        financial["net_profit_ttm"] - financial["operating_cash_flow_ttm"]
    ) / average_assets
    result["gross_margin"] = financial["gross_margin_pct"] / 100.0
    result["low_leverage"] = -financial["total_liabilities"] / financial[
        "total_assets"
    ].where(financial["total_assets"] > 0)
    result["profit_stability"] = financial["profit_stability"]
    finance_mask = universe["industry_name"].fillna("").str.contains(
        "银行|保险|证券|金融"
    )
    result.loc[finance_mask, [
        "ocf_to_assets",
        "accrual_quality",
        "low_leverage",
    ]] = np.nan
    return result
