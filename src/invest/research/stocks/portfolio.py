"""多因子排名、持仓缓冲和简单组合约束。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _normalize_with_cap(weights: pd.Series, cap: float) -> pd.Series:
    # 迭代归一化权重并限制单只股票上限。
    result = weights.clip(lower=0).astype(float)
    if result.sum() <= 0:
        return result
    result /= result.sum()
    for _ in range(20):
        over = result > cap + 1e-12
        if not over.any():
            break
        excess = (result.loc[over] - cap).sum()
        result.loc[over] = cap
        free = ~over
        if not free.any() or result.loc[free].sum() <= 0:
            break
        result.loc[free] += excess * result.loc[free] / result.loc[free].sum()
    return result


def _normalize_with_limits(weights: pd.Series, limits: pd.Series) -> pd.Series:
    # 在逐股票容量上限内尽可能将权重分配到百分之百。
    result = weights.clip(lower=0).astype(float).clip(upper=limits)
    target = min(1.0, float(limits.sum()))
    for _ in range(20):
        missing = target - float(result.sum())
        if missing <= 1e-12:
            break
        room = (limits - result).clip(lower=0)
        if room.sum() <= 0:
            break
        addition = missing * room / room.sum()
        result = (result + addition).clip(upper=limits)
    return result


def _select_codes(
    ranked: pd.DataFrame,
    current_holdings: pd.DataFrame | None,
    config: dict,
) -> list[str]:
    # 使用买入和持有双阈值选择目标股票。
    settings = config["portfolio"]
    total = len(ranked)
    buy_limit = max(1, int(np.ceil(total * settings["buy_rank_pct"])))
    hold_limit = max(1, int(np.ceil(total * settings["hold_rank_pct"])))
    held: set[str] = set()
    if current_holdings is not None and not current_holdings.empty:
        held = set(current_holdings["stock_code"].astype(str))
    kept = [code for code in ranked.index[:hold_limit] if code in held]
    candidates = [code for code in ranked.index[:buy_limit] if code not in kept]
    return (kept + candidates)[: int(settings["target_count"])]


def _limit_industries(
    weights: pd.Series,
    industries: pd.Series,
    maximum: float,
) -> pd.Series:
    # 按比例削减超过绝对上限的行业并重新分配剩余权重。
    result = weights.copy()
    for _ in range(20):
        totals = result.groupby(industries).sum()
        excess_industries = totals.loc[totals > maximum + 1e-12]
        if excess_industries.empty:
            break
        removed = 0.0
        for industry, total in excess_industries.items():
            mask = industries == industry
            scale = maximum / total
            removed += result.loc[mask].sum() * (1.0 - scale)
            result.loc[mask] *= scale
        free = ~industries.isin(excess_industries.index)
        if removed <= 0 or result.loc[free].sum() <= 0:
            break
        result.loc[free] += removed * result.loc[free] / result.loc[free].sum()
    return result


def build_portfolio(
    scores: pd.DataFrame,
    current_holdings: pd.DataFrame | None,
    config: dict,
) -> pd.DataFrame:
    # 从有效综合分生成带简单集中度和容量约束的目标组合。
    ranked = scores.loc[scores["total_score"].notna()].sort_values(
        "total_score",
        ascending=False,
    ).copy()
    ranked["rank_no"] = np.arange(1, len(ranked) + 1)
    selected_codes = _select_codes(ranked, current_holdings, config)
    selected = ranked.loc[selected_codes].copy()
    if selected.empty:
        return pd.DataFrame(columns=[
            "stock_code",
            "rank_no",
            "target_weight",
            "total_score",
            "industry_code",
            "constraint_notes",
        ])
    base_ratio = float(config["portfolio"]["base_weight_ratio"])
    base = pd.Series(1.0 / len(selected), index=selected.index)
    tilt = (selected["total_score"] - selected["total_score"].min()).clip(lower=0)
    tilt = tilt / tilt.sum() if tilt.sum() > 0 else base
    weights = base_ratio * base + (1.0 - base_ratio) * tilt
    maximum = float(config["portfolio"]["max_stock_weight"])
    weights = _normalize_with_cap(weights, maximum)
    weights = _limit_industries(
        weights,
        selected["industry_code"].fillna("UNKNOWN"),
        float(config["portfolio"]["max_industry_weight"]),
    )
    account_value = float(config["execution"]["initial_cash"])
    capacity = (
        selected["adv_20"] * float(config["portfolio"]["max_adv_participation"])
        / account_value
    )
    limits = capacity.clip(upper=maximum)
    weights = _normalize_with_limits(weights, limits)
    selected["target_weight"] = weights
    selected["constraint_notes"] = ""
    selected.index.name = "stock_code"
    return selected.reset_index()[[
        "stock_code",
        "rank_no",
        "target_weight",
        "total_score",
        "industry_code",
        "constraint_notes",
    ]]
