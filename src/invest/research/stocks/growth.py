"""基本面成长因子模型。"""

from __future__ import annotations

import pandas as pd


WEIGHTS = {
    "revenue_yoy": 0.25,
    "parent_profit_yoy": 0.30,
    "deduct_profit_yoy": 0.20,
    "revenue_cagr_3y": 0.15,
    "growth_stability": 0.10,
}


def calculate(data: dict, config: dict) -> pd.DataFrame:
    # 返回收入、利润、长期成长和成长稳定性因子。
    columns = list(WEIGHTS)
    return data["financial"].reindex(data["universe"].index)[columns].copy()
