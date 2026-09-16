"""因子去极值、标准化和行业市值中性化。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def clean_factor(series: pd.Series) -> pd.Series:
    # 将因子转换为有限浮点数并保留合理缺失。
    values = pd.to_numeric(series, errors="coerce").astype(float)
    return values.where(np.isfinite(values))


def winsorize_mad(series: pd.Series, multiple: float = 3.0) -> pd.Series:
    # 使用中位数绝对偏差缩尾，MAD 为零时保持原值。
    values = clean_factor(series)
    median = values.median()
    mad = (values - median).abs().median()
    if pd.isna(median) or pd.isna(mad) or mad == 0:
        return values
    scale = 1.4826 * mad
    return values.clip(median - multiple * scale, median + multiple * scale)


def rank_normalize(series: pd.Series) -> pd.Series:
    # 将横截面秩转换到 [-1, 1] 区间。
    values = clean_factor(series)
    count = values.notna().sum()
    if count <= 1:
        return values * 0.0
    percentile = values.rank(method="average", pct=True)
    return percentile * 2.0 - 1.0


def neutralize(
    series: pd.Series,
    industry: pd.Series,
    log_float_cap: pd.Series,
    use_industry: bool = True,
    use_size: bool = True,
) -> pd.Series:
    # 对行业和对数流通市值回归并返回标准化残差。
    frame = pd.DataFrame({
        "factor": clean_factor(series),
        "industry": industry.fillna("UNKNOWN").astype(str),
        "size": clean_factor(log_float_cap),
    }).dropna(subset=["factor"])
    if use_size:
        frame = frame.dropna(subset=["size"])
    if len(frame) < 5:
        return rank_normalize(series)
    parts = [np.ones((len(frame), 1))]
    if use_industry:
        dummies = pd.get_dummies(frame["industry"], drop_first=True, dtype=float)
        if not dummies.empty:
            parts.append(dummies.to_numpy())
    if use_size:
        parts.append(frame[["size"]].to_numpy())
    design = np.column_stack(parts)
    target = frame["factor"].to_numpy()
    coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    residual = target - design @ coefficients
    scale = residual.std(ddof=1)
    normalized = residual / scale if scale > 0 else residual * 0.0
    result = pd.Series(np.nan, index=series.index, dtype=float)
    result.loc[frame.index] = normalized
    return result


def process_factor(
    series: pd.Series,
    universe: pd.DataFrame,
    config: dict,
) -> pd.Series:
    # 按统一顺序完成缩尾、秩变换和中性化。
    eligible = universe["universe_flag"] == 1
    output = pd.Series(np.nan, index=universe.index, dtype=float)
    ranked = rank_normalize(
        winsorize_mad(
            series.reindex(universe.index).loc[eligible],
            float(config["preprocess"]["mad_multiple"]),
        )
    )
    float_cap = universe.loc[eligible, "float_cap"].where(
        universe.loc[eligible, "float_cap"] > 0
    )
    adjusted = neutralize(
        ranked,
        universe.loc[eligible, "industry_code"],
        np.log(float_cap),
        bool(config["preprocess"]["industry_neutral"]),
        bool(config["preprocess"]["size_neutral"]),
    )
    output.loc[eligible] = adjusted
    return output
