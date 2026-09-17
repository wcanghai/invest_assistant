"""低波动、低回撤和低特异波动因子模型。"""

from __future__ import annotations

import numpy as np
import pandas as pd


WEIGHTS = {
    "volatility_60d": 0.35,
    "downside_vol_60d": 0.25,
    "max_drawdown_120d": 0.20,
    "idiosyncratic_vol_120d": 0.20,
}


def _max_drawdown(prices: pd.Series) -> float:
    # 计算价格序列最大回撤并返回负的绝对值。
    valid = prices.dropna()
    if len(valid) < 60:
        return np.nan
    drawdown = valid / valid.cummax() - 1.0
    return float(drawdown.min())


def _market_returns(bars: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    # 构建每日等权市场和行业收益，供特异波动回归使用。
    merged = bars.merge(
        universe[["industry_code"]],
        left_on="stock_code",
        right_index=True,
        how="left",
    )
    market = merged.groupby("trade_date")["return"].mean().rename("market_return")
    industry = merged.groupby(["trade_date", "industry_code"])["return"].mean()
    merged = merged.join(market, on="trade_date")
    merged = merged.join(industry.rename("industry_return"), on=[
        "trade_date",
        "industry_code",
    ])
    return merged


def _idio_vol(group: pd.DataFrame) -> float:
    # 回归市场和行业收益并返回年化残差波动的负值。
    sample = group.tail(120).dropna(
        subset=["return", "market_return", "industry_return"]
    )
    if len(sample) < 80:
        return np.nan
    design = np.column_stack([
        np.ones(len(sample)),
        sample["market_return"].to_numpy(),
        sample["industry_return"].to_numpy(),
    ])
    target = sample["return"].to_numpy()
    coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    residual = target - design @ coefficients
    return float(-np.std(residual, ddof=1) * np.sqrt(252))


def calculate(data: dict, config: dict) -> pd.DataFrame:
    # 计算股票历史波动、下行波动、回撤和特异波动。
    enriched = _market_returns(data["bars"], data["universe"])
    rows: list[dict] = []
    for stock_code, group in enriched.groupby("stock_code"):
        ordered = group.sort_values("trade_date")
        returns = ordered["return"]
        recent_60 = returns.tail(60).dropna()
        downside = recent_60.loc[recent_60 < 0]
        rows.append({
            "stock_code": stock_code,
            "volatility_60d": -recent_60.std() * np.sqrt(252),
            "downside_vol_60d": -downside.std() * np.sqrt(252),
            "max_drawdown_120d": _max_drawdown(ordered["adj_close"].tail(120)),
            "idiosyncratic_vol_120d": _idio_vol(ordered),
        })
    if not rows:
        return pd.DataFrame(index=data["universe"].index, columns=list(WEIGHTS))
    return pd.DataFrame(rows).set_index("stock_code").reindex(data["universe"].index)
