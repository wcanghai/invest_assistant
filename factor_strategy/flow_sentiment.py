"""资金流与市场情绪因子模型。"""

from __future__ import annotations

import numpy as np
import pandas as pd


WEIGHTS = {
    "financing_flow_20d": 0.25,
    "northbound_change_20d": 0.25,
    "shareholder_change": 0.20,
    "low_pledge": 0.15,
    "popularity_trend": 0.10,
    "limit_order_strength": 0.05,
}


def _change(values: pd.Series, periods: int = 20) -> float:
    # 计算有效序列首尾相对变化。
    valid = pd.to_numeric(values, errors="coerce").dropna().tail(periods + 1)
    if len(valid) < 2 or valid.iloc[0] == 0:
        return np.nan
    return float(valid.iloc[-1] / valid.iloc[0] - 1.0)


def calculate(data: dict, config: dict) -> pd.DataFrame:
    # 计算融资、北向、股东、质押、人气和封单因子。
    universe = data["universe"]
    rows: list[dict] = []
    for stock_code, group in data["trade"].groupby("stock_code"):
        ordered = group.sort_values("trade_date")
        recent = ordered.tail(20)
        float_cap = universe["float_cap"].get(stock_code, np.nan)
        float_shares = universe["float_shares"].get(stock_code, np.nan)
        financing = pd.to_numeric(
            recent["financing_net_buy_10k"], errors="coerce"
        ).sum(min_count=1) * 10_000
        shareholders = pd.to_numeric(
            ordered["shareholder_count"], errors="coerce"
        ).dropna().drop_duplicates()
        popularity = -_change(ordered["market_popularity_rank"], 20)
        latest = ordered.iloc[-1]
        rows.append({
            "stock_code": stock_code,
            "financing_flow_20d": financing / float_cap if float_cap > 0 else np.nan,
            "northbound_change_20d": _change(
                ordered["northbound_holding_shares"] / float_shares, 20
            ) if float_shares > 0 else np.nan,
            "shareholder_change": -_change(shareholders, 2),
            "low_pledge": -float(latest.get("pledge_ratio_pct", np.nan)),
            "popularity_trend": popularity,
            "limit_order_strength": (
                float(latest.get("limit_order_amount_10k", np.nan)) * 10_000 / float_cap
                if float_cap > 0 else np.nan
            ),
        })
    if not rows:
        return pd.DataFrame(index=universe.index, columns=list(WEIGHTS))
    return pd.DataFrame(rows).set_index("stock_code").reindex(universe.index)
