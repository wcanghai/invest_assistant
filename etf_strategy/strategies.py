"""Five deterministic target-weight policies; no accounting or broker effects."""

import numpy as np
import pandas as pd

from .config import CLASSES, STRATEGIES
from .storage import clean
from .rotation import select_rotation


def covariance(returns, config):
    # Use common observed sessions only and a fixed diagonal shrinkage.
    sample = returns.tail(config["cov_window"]).dropna(how="any")
    if len(sample) < config["min_cov_observations"]:
        raise ValueError("insufficient_common_observations")
    cov = sample.cov().to_numpy() * 252
    if not np.isfinite(cov).all() or (np.diag(cov) <= 1e-14).any():
        raise ValueError("invalid_covariance")
    shrink = config["shrinkage"]
    return (1 - shrink) * cov + shrink * np.diag(np.diag(cov))


def equal_risk(cov):
    # Solve convex risk budgeting by coordinate descent and verify risk-contribution residual.
    if not np.isfinite(cov).all() or np.linalg.eigvalsh(cov).min() <= 0:
        raise ValueError("covariance_not_positive_definite")
    n = len(cov)
    weights = 1 / np.sqrt(np.diag(cov))
    budget = np.ones(n) / n
    residual = float("inf")
    for iteration in range(10000):
        for i in range(n):
            cross = cov[i] @ weights - cov[i, i] * weights[i]
            weights[i] = (-cross + np.sqrt(cross * cross + 4 * cov[i, i] * budget[i]))
            weights[i] /= 2 * cov[i, i]
        contributions = weights * (cov @ weights)
        residual = float(np.max(np.abs(contributions / contributions.sum() - budget)))
        if residual < 1e-8:
            return weights / weights.sum(), {"residual": residual, "iterations": iteration + 1}
    raise ValueError(f"risk_parity_nonconvergence:{residual}")


def features(dataset, day, codes):
    # Compute trailing signals without filling gaps or observing future prices.
    cfg = dataset.config
    prices = dataset.prices.loc[:day, codes]
    out = pd.DataFrame(index=codes)
    out["momentum"] = np.nan
    windows = cfg["momentum_windows"]
    if len(prices) > max(windows):
        out["momentum"] = sum(prices.iloc[-1] / prices.iloc[-1 - w] - 1
                              for w in windows) / len(windows)
    ma = prices.tail(cfg["ma_window"])
    out["trend"] = (prices.iloc[-1] > ma.mean()) & ma.notna().all()
    ret = dataset.returns.loc[:day, codes]
    out["volatility"] = ret.tail(cfg["vol_window"]).std() * np.sqrt(252)
    enough = ret.tail(cfg["vol_window"]).notna().sum() >= cfg["vol_window"]
    out.loc[~enough, "volatility"] = np.nan
    return out


def risk_controls(weights, dataset, day, classes):
    # Apply only explicitly configured limits; released capital stays in cash.
    cfg = dataset.config
    weights = dict(weights)
    target = cfg["target_volatility"]
    if target and weights:
        codes = sorted(weights)
        cov = covariance(dataset.returns.loc[:day, codes], cfg)
        values = np.array([weights[c] for c in codes])
        vol = float(np.sqrt(values @ cov @ values))
        scale = min(1, target / vol) if vol > 0 else 1
        weights = {c: w * scale for c, w in weights.items()}
    cap = cfg["max_asset_weight"]
    if cap is not None:
        weights = {c: min(w, cap) for c, w in weights.items()}
    for category, limit in cfg["class_caps"].items():
        codes = [c for c in weights if classes[c] == category]
        total = sum(weights[c] for c in codes)
        if total > limit:
            for code in codes:
                weights[code] *= limit / total
    return {c: float(w) for c, w in weights.items() if w > 1e-12}


def target(dataset, day, strategy, universe=None, holdings=None):
    # Return auditable status, selected weights and feature details for one policy.
    if strategy not in STRATEGIES:
        raise ValueError("Unknown strategy")
    pool = "factor" if strategy == "factor_rotation" else dataset.config["universe_pool"]
    if strategy in ("multi_rotation", "equity_rotation"):
        pool = "all_equity" if strategy == "equity_rotation" else "all_multi"
    supplied = universe is not None
    universe = dataset.universe(day, pool) if universe is None else universe
    selected = universe[universe.eligible].copy()
    if strategy == "factor_rotation" and not selected.empty:
        selected = selected[selected.market.eq(dataset.config["factor_market"])]
        selected = selected[selected.asset_class.eq("equity")]
    result = {"strategy": strategy, "signal_date": day, "status": "ok", "weights": {},
              "cash_weight": 1.0, "features": {}, "diagnostics": {},
              "quality_level": "exploratory", "universe": clean(universe.to_dict("records"))
              if supplied else dataset.universe_records(day, pool, universe)}
    if strategy == "factor_rotation":
        styles = set(selected.factor_style) if "factor_style" in selected else set()
        if not {"value", "quality", "low_vol"}.issubset(styles):
            result.update(status="not_eligible", reason="three_pure_styles_required")
            return result
        selected = selected.sort_values(["adv60_10k", "etf_code"], ascending=[False, True])
        selected = selected.drop_duplicates("factor_style")
    if selected.empty:
        if dataset.config["price_mode"] != "exploratory" or pool.startswith("all_"):
            result.update(status="blocked", reason="no_validated_eligible_assets")
        return result
    codes = sorted(selected.etf_code)
    classes = selected.set_index("etf_code").asset_class.to_dict()
    feat = features(dataset, day, codes)
    weights = {}
    try:
        if strategy in ("multi_rotation", "equity_rotation"):
            if dataset.config["price_mode"] == "exploratory":
                raise ValueError("rotation_requires_verified_signal_prices")
            weights, result["diagnostics"] = select_rotation(
                dataset, day, selected, feat, holdings)
        elif strategy == "buy_hold":
            if len(codes) != 1:
                raise ValueError("buy_hold_requires_one_frozen_code")
            weights = {codes[0]: 1.0}
        elif strategy in ("dual_momentum", "factor_rotation"):
            eligible = feat[feat.trend & feat.momentum.gt(0)].copy()
            if strategy == "dual_momentum":
                eligible = eligible[eligible.volatility.gt(1e-8)]
            eligible["code"] = eligible.index
            chosen = eligible.sort_values(["momentum", "code"], ascending=[False, True])
            chosen = chosen.head(3 if strategy == "dual_momentum" else 2)
            if len(chosen) and strategy == "dual_momentum":
                inverse = 1 / chosen.volatility
                weights = (inverse / inverse.sum() * (len(chosen) / 3)).to_dict()
            elif len(chosen):
                weights = {c: 0.5 for c in chosen.index}
        elif strategy in ("trend", "strategic"):
            for category in CLASSES:
                group = [c for c in codes if classes[c] == category]
                for code in group:
                    if strategy == "strategic" or feat.at[code, "trend"]:
                        weights[code] = 0.25 / len(group)
        else:
            returns = dataset.returns.loc[:day, codes]
            cov = covariance(returns, dataset.config)
            groups = [[c for c in codes if classes[c] == kind] for kind in CLASSES]
            groups = [g for g in groups if g]
            allocation = np.zeros((len(codes), len(groups)))
            diagnostics = []
            for j, group in enumerate(groups):
                indices = [codes.index(c) for c in group]
                inner, diag = equal_risk(cov[np.ix_(indices, indices)])
                allocation[indices, j] = inner
                diagnostics.append(diag)
            outer, diag = equal_risk(allocation.T @ cov @ allocation)
            result["diagnostics"] = {"inner": diagnostics, "outer": diag}
            base = allocation @ outer
            weights = {c: float(base[i]) for i, c in enumerate(codes) if feat.at[c, "trend"]}
        base_weights = weights.copy()
        weights = risk_controls(weights, dataset, day, classes)
        total = sum(base_weights.values())
        result["diagnostics"]["risk_scale"] = sum(weights.values()) / total if total else 1.0
        result["diagnostics"]["base_weights"] = base_weights
    except ValueError as exc:
        result.update(status="blocked", reason=str(exc))
        return result
    result.update(weights=weights, cash_weight=max(0.0, 1 - sum(weights.values())),
                  features=clean(feat.to_dict("index")),
                  quality_level=dataset.quality(day, codes))
    if (dataset.config["price_mode"] == "validated"
            and result["quality_level"] != "validated_research"):
        result.update(status="blocked", reason="historical_evidence_incomplete",
                      weights={}, cash_weight=1.0)
    return result
