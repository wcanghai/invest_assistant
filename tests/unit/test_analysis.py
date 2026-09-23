from invest.core.settings import load_settings
from invest.research.analysis import _linear_score, analyze


def test_stock_analysis_uses_local_facts():
    settings = load_settings()
    result = analyze(settings.databases["market"], settings.databases["research"], "stock", "000001")
    assert result["security"]["code"].startswith("000001.")
    assert result["quant_score"] is not None
    assert result["evidence"]


def test_etf_missing_values_are_warnings():
    settings = load_settings()
    result = analyze(settings.databases["market"], settings.databases["research"], "etf", "510300")
    assert result["security"]["code"].startswith("510300.")
    assert any("unit_nav" in warning for warning in result["warnings"])


def test_risk_score_falls_when_drawdown_grows():
    assert _linear_score(10, 0, 50, higher_is_better=False) > _linear_score(
        30, 0, 50, higher_is_better=False
    )


def test_missing_metric_is_not_scored_as_zero():
    assert _linear_score(None, 25, 0) is None
    assert _linear_score(0, 25, 0) == 0


def test_evidence_ids_are_unique_between_runs():
    settings = load_settings()
    first = analyze(settings.databases["market"], settings.databases["research"], "stock", "000001")
    second = analyze(settings.databases["market"], settings.databases["research"], "stock", "000001")
    assert {item["id"] for item in first["evidence"]}.isdisjoint(
        item["id"] for item in second["evidence"]
    )
