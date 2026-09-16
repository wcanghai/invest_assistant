"""七类因子预处理、类别得分和总分合成。"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pandas as pd

from . import flow_sentiment, growth, liquidity, low_risk, momentum, quality, value
from .preprocess import process_factor


MODELS = {
    "value": value,
    "quality": quality,
    "growth": growth,
    "momentum": momentum,
    "low_risk": low_risk,
    "flow_sentiment": flow_sentiment,
    "liquidity": liquidity,
}


def _weighted_score(
    frame: pd.DataFrame,
    weights: dict[str, float],
    minimum_ratio: float,
) -> pd.Series:
    # 按每行有效子因子重新归一化权重并计算大类分。
    available = frame.notna()
    weight_series = pd.Series(weights, dtype=float).reindex(frame.columns)
    numerator = frame.mul(weight_series, axis=1).sum(axis=1, min_count=1)
    denominator = available.mul(weight_series, axis=1).sum(axis=1)
    minimum = math.ceil(len(weight_series) * minimum_ratio)
    score = numerator / denominator.where(denominator > 0)
    return score.where(available.sum(axis=1) >= minimum)


def _json_number(value: Any) -> float | None:
    # 将因子标量转换为 JSON 可安全保存的浮点数。
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def calculate_scores(data: dict, config: dict) -> pd.DataFrame:
    # 显式调用七个模型并生成大类分、总分和审计明细。
    universe = data["universe"]
    result = universe[[
        "universe_flag",
        "exclusion_reason",
        "industry_code",
        "industry_name",
        "float_cap",
        "adv_20",
    ]].copy()
    details: dict[str, dict[str, Any]] = {code: {} for code in universe.index}
    minimum_ratio = float(config["preprocess"]["min_category_valid_ratio"])
    category_scores: dict[str, pd.Series] = {}
    valid_counts = pd.Series(0, index=universe.index, dtype=int)
    for category, module in MODELS.items():
        raw = module.calculate(data, config).reindex(universe.index)
        processed = pd.DataFrame(index=universe.index)
        for factor_name in module.WEIGHTS:
            processed[factor_name] = process_factor(
                raw[factor_name],
                universe,
                config,
            )
            valid_counts += processed[factor_name].notna().astype(int)
            for code in universe.index:
                details[code][factor_name] = {
                    "raw": _json_number(raw.at[code, factor_name]),
                    "score": _json_number(processed.at[code, factor_name]),
                }
        category_scores[category] = _weighted_score(
            processed,
            module.WEIGHTS,
            minimum_ratio,
        )
        result[f"{category}_score"] = category_scores[category]
    weights = pd.Series(config["category_weights"], dtype=float)
    category_frame = pd.DataFrame(category_scores)
    numerator = category_frame.mul(weights, axis=1).sum(axis=1, min_count=1)
    denominator = category_frame.notna().mul(weights, axis=1).sum(axis=1)
    result["total_score"] = numerator / denominator.where(denominator > 0)
    core_missing = category_frame[[
        "value",
        "quality",
        "growth",
        "momentum",
    ]].isna().sum(axis=1)
    result.loc[core_missing >= 2, "total_score"] = np.nan
    result.loc[result["universe_flag"] != 1, "total_score"] = np.nan
    result["valid_factor_count"] = valid_counts
    result["factor_detail_json"] = [
        json.dumps(details[code], ensure_ascii=False, sort_keys=True)
        for code in result.index
    ]
    result.index.name = "stock_code"
    return result
