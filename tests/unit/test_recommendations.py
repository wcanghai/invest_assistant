"""Tests for current stock recommendation calculation and explanations."""

from __future__ import annotations

import pandas as pd

from invest.reporting.recommendations import PROFILE_WEIGHTS
from invest.reporting.recommendations import _exclusion_counts
from invest.reporting.recommendations import _factor_summary
from invest.reporting.recommendations import _profile_score


def _scores() -> pd.DataFrame:
    # 构造可以区分稳健与进取偏好的固定因子样本。
    rows = {
        "steady": [1, "", 2, 2, 0, 0, 3, 0, 1],
        "growth": [1, "", 0, 0, 3, 3, -1, 2, 1],
        "excluded": [0, "ST", 5, 5, 5, 5, 5, 5, 5],
        "missing": [1, "", None, None, 2, None, 1, 1, 1],
    }
    columns = [
        "universe_flag", "exclusion_reason", "value_score", "quality_score",
        "growth_score", "momentum_score", "low_risk_score",
        "flow_sentiment_score", "liquidity_score",
    ]
    return pd.DataFrame.from_dict(rows, orient="index", columns=columns)


def test_profiles_have_distinct_preferences_and_apply_core_rules():
    # 稳健型偏好质量低风险，进取型偏好成长动量，并排除无效样本。
    scores = _scores()
    conservative = _profile_score(scores, PROFILE_WEIGHTS["conservative"])
    aggressive = _profile_score(scores, PROFILE_WEIGHTS["aggressive"])
    assert conservative["steady"] > conservative["growth"]
    assert aggressive["growth"] > aggressive["steady"]
    assert pd.isna(conservative["excluded"])
    assert pd.isna(conservative["missing"])


def test_explanations_and_exclusion_summary_come_from_factors():
    # 优势、弱项和排除统计都由真实输入确定。
    scores = _scores()
    strengths, risks = _factor_summary(scores.loc["steady"])
    assert strengths[0] == "低风险因子较强"
    assert "成长因子相对偏弱" in risks
    counts = _exclusion_counts(scores)
    assert counts["ST"] == 1
    assert counts["CORE_FACTOR_MISSING"] == 1


def test_profile_weights_are_complete_and_normalized():
    # 三种类型均覆盖七类因子且权重之和为一。
    expected = {
        "value", "quality", "growth", "momentum", "low_risk",
        "flow_sentiment", "liquidity",
    }
    for weights in PROFILE_WEIGHTS.values():
        assert set(weights) == expected
        assert abs(sum(weights.values()) - 1.0) < 1e-12
