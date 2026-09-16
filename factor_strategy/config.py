"""多因子策略默认参数和 JSON 配置读取。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "model_version": "1.0.0",
    "benchmark": "CSI_A500",
    "universe": {
        "min_listing_days": 252,
        "lookback_days": 300,
        "min_trading_days_20": 15,
        "min_adv_10k": 2000.0,
        "exclude_bottom_liquidity_pct": 0.20,
    },
    "preprocess": {
        "mad_multiple": 3.0,
        "industry_neutral": True,
        "size_neutral": True,
        "min_category_valid_ratio": 0.50,
    },
    "category_weights": {
        "value": 0.20,
        "quality": 0.20,
        "growth": 0.15,
        "momentum": 0.20,
        "low_risk": 0.10,
        "flow_sentiment": 0.10,
        "liquidity": 0.05,
    },
    "portfolio": {
        "target_count": 50,
        "buy_rank_pct": 0.08,
        "hold_rank_pct": 0.15,
        "min_stock_weight": 0.005,
        "max_stock_weight": 0.03,
        "max_industry_active_weight": 0.05,
        "max_industry_weight": 0.25,
        "max_top10_weight": 0.30,
        "max_turnover": 0.30,
        "max_adv_participation": 0.05,
        "base_weight_ratio": 0.60,
    },
    "execution": {
        "initial_cash": 1_000_000.0,
        "pending_days": 3,
        "buy_fee_bps": 3.0,
        "sell_fee_bps": 3.0,
        "sell_tax_bps": 5.0,
        "impact_bps": 5.0,
        "lot_size": 100,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    # 递归合并用户配置和默认配置。
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_config(config: dict[str, Any]) -> None:
    # 校验关键权重和阈值，尽早阻止无效策略运行。
    weights = config["category_weights"]
    if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
        raise ValueError("因子大类权重之和必须等于 1")
    portfolio = config["portfolio"]
    if not 0 < portfolio["buy_rank_pct"] <= portfolio["hold_rank_pct"] <= 1:
        raise ValueError("买入排名阈值必须小于等于持有阈值")
    if portfolio["target_count"] <= 0:
        raise ValueError("目标持仓数必须大于零")
    if not 0 < portfolio["max_stock_weight"] <= 1:
        raise ValueError("单股权重上限必须位于 (0, 1]")
    if config["universe"]["lookback_days"] < 252:
        raise ValueError("行情回看窗口不能少于 252 个交易日")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    # 读取 JSON 配置、补充默认值并执行校验。
    override: dict[str, Any] = {}
    if path is not None:
        override = json.loads(Path(path).read_text(encoding="utf-8"))
    config = _deep_merge(DEFAULT_CONFIG, override)
    validate_config(config)
    return config


def config_hash(config: dict[str, Any]) -> str:
    # 生成稳定的配置内容哈希。
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
