"""Tests for exploratory ETF recommendation calculations."""

from __future__ import annotations

import pandas as pd

from invest.reporting.etf_recommendations import PROFILE_WEIGHTS
from invest.reporting.etf_recommendations import _feature_row
from invest.reporting.etf_recommendations import _profile_score
from invest.reporting.etf_recommendations import _score_features


def _frame() -> pd.DataFrame:
    # 构造满足窗口长度且无异常价格跳变的 ETF 日线样本。
    dates = pd.date_range("2025-01-01", periods=300, freq="B").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "etf_code": "510300.SH",
        "trade_date": dates,
        "close": [3 + index * 0.01 for index in range(300)],
        "amount_10k": 3000.0,
        "is_trading": 1,
        "forward_factor": 1.0,
    })


def test_feature_gate_accepts_liquid_complete_etf_and_rejects_bad_jump():
    # 合格样本通过基础门槛，未解释的四成跳变会被明确排除。
    frame = _frame()
    good = _feature_row(frame, set())
    assert good["eligible"] is True
    assert good["return_252"] > 0
    jumped = frame.copy()
    jumped.loc[250:, "close"] *= 2
    bad = _feature_row(jumped, set())
    assert "UNEXPLAINED_PRICE_JUMP" in bad["reasons"]


def test_profile_weights_prefer_low_risk_or_momentum_as_configured():
    # 三种固定权重对低风险和动量的偏好必须可区分。
    frame = pd.DataFrame({
        "eligible": [True, True],
        "momentum_score": [0.1, 0.9],
        "trend_score": [0.2, 0.8],
        "low_risk_score": [0.9, 0.1],
        "liquidity_score": [0.5, 0.5],
    })
    conservative = _profile_score(frame, PROFILE_WEIGHTS["conservative"])
    aggressive = _profile_score(frame, PROFILE_WEIGHTS["aggressive"])
    assert conservative.iloc[0] > conservative.iloc[1]
    assert aggressive.iloc[1] > aggressive.iloc[0]


def test_factor_scoring_produces_all_four_categories():
    # 原始特征均存在时，四类可审计得分均应有限。
    rows = []
    for index in range(3):
        rows.append({
            "return_20": 0.02 + index * 0.01,
            "return_60": 0.03 + index * 0.01,
            "return_126": 0.04 + index * 0.01,
            "return_252": 0.05 + index * 0.01,
            "trend_20": 0.01 + index * 0.01,
            "trend_60": 0.02 + index * 0.01,
            "trend_200": 0.03 + index * 0.01,
            "volatility_60": 0.30 - index * 0.01,
            "downside_volatility_60": 0.20 - index * 0.01,
            "max_drawdown_120": -0.30 + index * 0.01,
            "adv20_10k": 2000 + index * 500,
            "coverage_253": 0.96 + index * 0.01,
            "amount_cv_20": 0.30 - index * 0.02,
        })
    scored = _score_features(pd.DataFrame(rows))
    columns = ["momentum_score", "trend_score", "low_risk_score", "liquidity_score"]
    assert scored[columns].notna().all().all()


def test_profile_weights_are_normalized_and_complete():
    # 每种 ETF 类型均覆盖四类因子，且总权重精确为一。
    expected = {"momentum", "trend", "low_risk", "liquidity"}
    for weights in PROFILE_WEIGHTS.values():
        assert set(weights) == expected
        assert abs(sum(weights.values()) - 1.0) < 1e-12
