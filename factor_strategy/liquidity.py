"""成交冲击和成交稳定性因子模型。"""

from __future__ import annotations

import numpy as np
import pandas as pd


WEIGHTS = {
    "amihud_20d": 0.40,
    "amount_20d": 0.30,
    "amount_stability": 0.20,
    "turnover_stability": 0.10,
}


def calculate(data: dict, config: dict) -> pd.DataFrame:
    # 计算价格冲击、平均成交额和换手稳定性。
    universe = data["universe"]
    rows: list[dict] = []
    for stock_code, group in data["bars"].groupby("stock_code"):
        recent = group.sort_values("trade_date").tail(20)
        amount = recent["amount"].where(recent["amount"] > 0)
        float_shares = universe["float_shares"].get(stock_code, np.nan)
        turnover = recent["volume"] / float_shares if float_shares > 0 else np.nan
        rows.append({
            "stock_code": stock_code,
            "amihud_20d": -(recent["return"].abs() / amount).mean(),
            "amount_20d": np.log(amount.mean()) if amount.mean() > 0 else np.nan,
            "amount_stability": -np.log(amount).std(),
            "turnover_stability": -pd.Series(turnover).std(),
        })
    if not rows:
        return pd.DataFrame(index=universe.index, columns=list(WEIGHTS))
    return pd.DataFrame(rows).set_index("stock_code").reindex(universe.index)
