"""Versioned research parameters, with no assumed account capital."""

import copy
import hashlib
import json
import math


DEFAULTS = {
    "version": "1.0.0", "price_mode": "exploratory", "universe_mode": "survivors",
    "min_history": 253, "coverage_window": 253, "min_coverage": 0.95,
    "liquidity_window": 20, "min_trading_days": 18, "min_adv_10k": 2000.0,
    "dedup_window": 60, "momentum_windows": [126, 252], "ma_window": 200,
    "vol_window": 60, "cov_window": 126, "min_cov_observations": 100,
    "shrinkage": 0.10, "fee_bps": 3.0, "slippage_bps": 5.0,
    "target_volatility": None, "max_asset_weight": None, "class_caps": {},
    "factor_market": "CN", "cash_return": 0.0,
    "universe_pool": "base", "rotation_constraints": True, "rotation_buffer": True,
    "execution_delay": 0, "allowed_codes": None,
}
LEGACY_STRATEGIES = ("dual_momentum", "trend", "risk_parity", "strategic", "factor_rotation")
STRATEGIES = LEGACY_STRATEGIES + ("multi_rotation", "equity_rotation", "buy_hold")
CLASSES = ("equity", "bond", "gold", "commodity")


def load_config(path=None, overrides=None):
    # Merge explicit settings and reject invalid risk or lookback parameters.
    config = copy.deepcopy(DEFAULTS)
    changes = json.loads(path.read_text(encoding="utf-8")) if path else {}
    changes.update(overrides or {})
    if set(changes) - set(config):
        raise ValueError(f"Unknown config fields: {set(changes) - set(config)}")
    config.update(changes)
    if config["price_mode"] not in ("exploratory", "verified_prices", "validated"):
        raise ValueError("Invalid price_mode")
    if config["universe_mode"] not in ("survivors", "point_in_time"):
        raise ValueError("Invalid universe_mode")
    if config["universe_pool"] not in ("base", "factor", "all_multi", "all_equity"):
        raise ValueError("Invalid universe pool")
    for key in ("rotation_constraints", "rotation_buffer"):
        if type(config[key]) is not bool:
            raise ValueError(f"Invalid {key}")
    if type(config["execution_delay"]) is not int or config["execution_delay"] not in (0, 1):
        raise ValueError("Execution delay must be zero or one extra session")
    if config["allowed_codes"] is not None and not isinstance(config["allowed_codes"], list):
        raise ValueError("allowed_codes must be a list or null")
    for key in ("min_history", "coverage_window", "liquidity_window", "min_trading_days",
                "dedup_window", "ma_window", "vol_window", "cov_window",
                "min_cov_observations"):
        if type(config[key]) is not int or config[key] < 2:
            raise ValueError(f"Invalid {key}")
    for window in config["momentum_windows"]:
        if type(window) is not int or window < 2:
            raise ValueError("Invalid momentum window")
    if len(config["momentum_windows"]) != 2:
        raise ValueError("Exactly two momentum windows required")
    if config["min_trading_days"] > config["liquidity_window"]:
        raise ValueError("Trading days exceed window")
    if config["min_cov_observations"] > config["cov_window"]:
        raise ValueError("Covariance minimum exceeds window")
    for key in ("min_coverage", "shrinkage"):
        if not 0 < config[key] <= 1:
            raise ValueError(f"Invalid {key}")
    for key in ("fee_bps", "slippage_bps", "min_adv_10k"):
        if not math.isfinite(config[key]) or config[key] < 0:
            raise ValueError(f"Invalid {key}")
    if max(config["fee_bps"], config["slippage_bps"]) >= 10000:
        raise ValueError("Costs must be below 10000 bps")
    for value in (config["target_volatility"], config["max_asset_weight"],
                  *config["class_caps"].values()):
        if value is not None and (not math.isfinite(value) or not 0 < value <= 1):
            raise ValueError("Risk parameters must be in (0,1]")
    if set(config["class_caps"]) - set(CLASSES) or config["cash_return"] != 0:
        raise ValueError("Unknown class or unsupported cash return")
    return config


def digest(value):
    # Hash stable JSON metadata for reproducible runs.
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
