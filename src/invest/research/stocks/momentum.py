"""价格动量与短期反转因子模型。"""

from __future__ import annotations

import numpy as np
import pandas as pd


WEIGHTS = {
    "momentum_12_1": 0.35,
    "momentum_6_1": 0.25,
    "momentum_3_1": 0.15,
    "trend_20d": 0.15,
    "reversal_5d": 0.10,
}


def _period_return(values: pd.Series, start_offset: int, end_offset: int = 0) -> float:
    # 使用倒数位置计算指定窗口复权收益。
    required = start_offset + 1
    valid = values.dropna()
    if len(valid) < required:
        return np.nan
    start = valid.iloc[-required]
    end = valid.iloc[-(end_offset + 1)]
    if start <= 0:
        return np.nan
    return float(end / start - 1.0)


def calculate(data: dict, config: dict) -> pd.DataFrame:
    # 计算跳过最近一个月的中期动量、趋势和反转。
    rows: list[dict] = []
    for stock_code, group in data["bars"].groupby("stock_code"):
        prices = group.sort_values("trade_date")["adj_close"]
        rows.append({
            "stock_code": stock_code,
            "momentum_12_1": _period_return(prices, 252, 21),
            "momentum_6_1": _period_return(prices, 126, 21),
            "momentum_3_1": _period_return(prices, 63, 21),
            "trend_20d": _period_return(prices, 20),
            "reversal_5d": -_period_return(prices, 5),
        })
    if not rows:
        return pd.DataFrame(index=data["universe"].index, columns=list(WEIGHTS))
    return pd.DataFrame(rows).set_index("stock_code").reindex(data["universe"].index)
