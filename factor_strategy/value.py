"""价值因子模型。"""

from __future__ import annotations

import numpy as np
import pandas as pd


WEIGHTS = {
    "earnings_yield": 0.25,
    "book_to_price": 0.20,
    "dividend_yield": 0.15,
    "ocf_yield": 0.20,
    "deduct_profit_yield": 0.20,
}


def calculate(data: dict, config: dict) -> pd.DataFrame:
    # 计算盈利、账面、股息和现金流价值因子。
    universe = data["universe"]
    financial = data["financial"].reindex(universe.index)
    result = pd.DataFrame(index=universe.index)
    total_cap = universe["total_cap"].where(universe["total_cap"] > 0)
    parent_profit = financial["parent_net_profit_ttm"]
    deduct_profit = financial["deduct_net_profit_ttm"]
    result["earnings_yield"] = parent_profit.where(parent_profit > 0) / total_cap
    result["book_to_price"] = financial["total_equity"].where(
        financial["total_equity"] > 0
    ) / total_cap
    result["ocf_yield"] = financial["operating_cash_flow_ttm"] / total_cap
    result["deduct_profit_yield"] = deduct_profit.where(deduct_profit > 0) / total_cap
    trade = data["trade"]
    result["dividend_yield"] = np.nan
    if not trade.empty and "dividend_yield_pct" in trade:
        latest = trade.sort_values("trade_date").groupby("stock_code").tail(1)
        dividend = latest.set_index("stock_code")["dividend_yield_pct"] / 100.0
        result["dividend_yield"] = dividend.reindex(result.index).where(dividend >= 0)
    return result
