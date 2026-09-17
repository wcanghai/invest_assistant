"""Economic invariants and actual-holding behavior for full-market rotation."""

import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tests.test_etf_strategy import market
from invest.research.etf.backtest import backtest
from invest.research.etf.config import load_config
from invest.research.etf.data import Dataset
from invest.research.etf.events import event_checks
from invest.research.etf.rotation import select_rotation
from invest.storage.etf import import_records
from invest.research.etf.strategies import target
from invest.research.etf.ten_year import phase_ledger
from invest.research.etf.full_market import report_run


def inputs():
    # Nine distinct synthetic exposures make buffer, ordering and cash constraints observable.
    codes = [f"ETF{i}" for i in range(9)]
    rng = np.random.default_rng(9)
    returns = pd.DataFrame(rng.normal(0, .01, (126, 9)), columns=codes)
    data = SimpleNamespace(config=load_config(), returns=returns)
    selected = pd.DataFrame({"etf_code": codes, "theme_group": [None] * 9})
    feat = pd.DataFrame({"trend": True, "momentum": np.arange(9, 0, -1)}, index=codes)
    return data, selected, feat


def test_retention_uses_actual_holdings_and_does_not_refill_from_rank_six():
    # Actual rank-eight holding is retained, rank-nine exits, and empty new slots stay cash.
    data, selected, feat = inputs()
    weights, detail = select_rotation(data, 125, selected, feat, {"ETF7": 2, "ETF8": 3})
    assert set(weights) == {"ETF7", "ETF0", "ETF1", "ETF2", "ETF3"}
    assert detail["exits"] == [{"code": "ETF8", "reason": "not_retained", "rank": 9}]
    fresh, _ = select_rotation(data, 125, selected, feat, {})
    assert "ETF7" not in fresh
    selected["theme_group"] = "same"
    constrained, detail = select_rotation(data, 125, selected, feat, {})
    assert sum(constrained.values()) == pytest.approx(.4)
    assert detail["empty_slots"] == 3


def test_correlation_and_pair_history_fail_closed():
    # Identical exposures and unknown common history cannot fill an additional slot.
    data, selected, feat = inputs()
    data.returns["ETF1"] = data.returns.ETF0
    data.returns.loc[:30, "ETF2"] = np.nan
    weights, detail = select_rotation(data, 125, selected, feat, {})
    assert set(weights) == {"ETF0", "ETF3", "ETF4"}
    reasons = {r["code"]: r["reason"] for r in detail["selection"]}
    assert reasons["ETF1"] == "correlated_exposure"
    assert reasons["ETF2"] == "insufficient_pair_history"


def test_513100_split_suspension_economics(market):
    # Replay the real 1:5 registration/resumption pattern with constant economic value.
    path, dates = market
    code = "000000.SH"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET open=10,close=10,high=10.1,low=9.9 "
                     "WHERE etf_code=?", (code,))
        conn.execute("UPDATE etf_daily SET is_trading=0,volume=0 WHERE etf_code=? "
                     "AND trade_date=?", (code, dates[302]))
        conn.execute("UPDATE etf_daily SET open=2,close=2,high=2.1,low=1.9,forward_factor=5 "
                     "WHERE etf_code=? AND trade_date>=?", (code, dates[303]))
    import_records(path, "etf_action", [{
        "event_id": "513100_actual_pattern", "etf_code": code, "record_date": dates[301],
        "ex_date": dates[302], "share_change_date": dates[302], "resume_date": dates[303],
        "pay_date": dates[302], "cash_per_share": 0, "share_multiplier": 5,
        "known_at": dates[290], "source": "513100 official 2022-01-04 1:5 announcement",
        "verified": 1}])
    certify_fixture(path, dates)
    data = Dataset(path, load_config(overrides={"fee_bps": 0, "slippage_bps": 0,
                                               "price_mode": "verified_prices"}))
    assert data.action_issues[code] == []
    assert data.prices.at[dates[301], code] == data.prices.at[dates[303], code]
    signal = {"signal_date": dates[300], "weights": {code: 1}, "universe": [],
              "cash_weight": 0, "status": "ok", "quality_level": "exploratory"}
    result = backtest(data, "trend", dates[300], dates[304], signals={dates[300]: signal})
    assert all(r["nav"] == pytest.approx(1) for r in result["curve"])
    assert result["curve"][2]["holdings"][code] == pytest.approx(.5)
    phases, _ = phase_ledger(result, data)
    assert phases.reconciliation_residual.abs().max() < 1e-10


def test_unexplained_split_withdraws_performance(market):
    # A held asset's unsupported split cannot publish a plausible but false CAGR.
    path, dates = market
    code = "000000.SH"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET close=close/5,open=open/5,high=high/5,low=low/5,"
                     "forward_factor=5 WHERE etf_code=? AND trade_date>=?", (code, dates[303]))
    data = Dataset(path, load_config())
    decision = {"signal_date": dates[300], "weights": {code: 1}, "status": "ok",
                "quality_level": "exploratory"}
    result = backtest(data, "trend", dates[300], dates[305], signals={dates[300]: decision})
    assert result["status"] == "publication_blocked"
    assert "cagr" not in result["metrics"]
    assert result["curve"] == []
    assert result["publication_issues"][0]["date"] == dates[303]


def certify_fixture(path, dates):
    # Fixture certificates are explicit and never reused to certify production data.
    records = [{"etf_code": f"{i:06d}.SH", "start_date": dates[0], "end_date": dates[-1],
                "validated_at": dates[-1], "source": "synthetic fixture known no actions",
                "factor_direction": "multiply", "actions_complete": 1,
                "identity_complete": 0, "trading_rules_verified": 0} for i in range(7)]
    import_records(path, "etf_validation", records)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_classification SET exposure_type='broad'")
        for kind, value in (("bond", "government"), ("gold", "physical"),
                            ("commodity", "futures")):
            conn.execute("UPDATE etf_classification SET exposure_type=? WHERE asset_class=?",
                         (value, kind))


def test_full_market_requires_evidence_but_not_complete_survivorship(market):
    # Price-verified survivor research is allowed without mislabeling it PIT validated research.
    path, dates = market
    cfg = load_config(overrides={"price_mode": "verified_prices", "rotation_constraints": False})
    data = Dataset(path, cfg)
    assert not data.universe(dates[300], "all_multi").eligible.any()
    certify_fixture(path, dates)
    data = Dataset(path, cfg)
    assert data.universe(dates[300], "all_multi").eligible.sum() == 7
    assert data.universe(dates[300], "all_equity").eligible.sum() == 4
    signal = target(data, dates[300], "multi_rotation", holdings={})
    assert signal["status"] == "ok"
    assert sum(signal["weights"].values()) == pytest.approx(1)
    assert signal["quality_level"] == "exploratory"
    assert target(data, dates[300], "equity_rotation")["cash_weight"] == pytest.approx(.2)


def test_volatility_only_reduces_and_covariance_failure_preserves_target(market):
    # A high-volatility portfolio scales down, while missing shared history cancels new targets.
    path, dates = market
    certify_fixture(path, dates)
    cfg = load_config(overrides={"price_mode": "verified_prices", "target_volatility": .1,
                                "rotation_constraints": False})
    data = Dataset(path, cfg)
    data.returns *= 1000
    signal = target(data, dates[300], "multi_rotation")
    assert signal["status"] == "ok"
    assert 0 < sum(signal["weights"].values()) < 1
    assert 0 < signal["diagnostics"]["risk_scale"] < 1
    data.returns.loc[dates[174]:dates[220]] = np.nan
    blocked = target(data, dates[300], "multi_rotation")
    assert blocked["status"] == "blocked"
    assert blocked["weights"] == {}


def test_future_data_and_extra_execution_day(market):
    # Extra delay uses another exchange session; future data cannot rewrite past selection.
    path, dates = market
    certify_fixture(path, dates)
    cfg = load_config(overrides={"price_mode": "verified_prices", "rotation_constraints": False,
                                "execution_delay": 1})
    data = Dataset(path, cfg)
    signal = target(data, dates[300], "equity_rotation")
    result = backtest(data, "equity_rotation", dates[300], dates[304],
                      signals={dates[300]: signal})
    assert all(t["date"] == dates[302] for t in result["trades"])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET close=999,amount_10k=1 WHERE trade_date>?",
                     (dates[300],))
    changed = target(Dataset(path, cfg), dates[300], "equity_rotation")
    assert changed["weights"] == signal["weights"]
    assert changed["features"] == signal["features"]


def test_held_certificate_expiry_blocks_publication(market):
    # Continuing to hold after price evidence expires cannot silently publish an entire backtest.
    path, dates = market
    certify_fixture(path, dates)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_validation SET end_date=?", (dates[302],))
    data = Dataset(path, load_config(overrides={"price_mode": "verified_prices",
                                               "rotation_constraints": False}))
    signal = target(data, dates[300], "equity_rotation")
    result = backtest(data, "equity_rotation", dates[300], dates[305],
                      signals={dates[300]: signal})
    assert result["status"] == "publication_blocked"
    assert any(i["reason"] == "held_price_evidence_missing"
               for i in result["publication_issues"])


def test_report_outputs_reconcile_and_preserve_stage_evidence(market, tmp_path):
    # Exercise the full report path with certified synthetic data independently of production gaps.
    path, dates = market
    certify_fixture(path, dates)
    data = Dataset(path, load_config(overrides={"price_mode": "verified_prices",
                                               "rotation_constraints": False}))
    run = backtest(data, "equity_rotation", dates[260], dates[-1])
    assert run["status"] == "evaluated"
    folder = tmp_path / "synthetic_report"
    report_run(folder, "SYNTHETIC_QA", run, data, {})
    weights = pd.read_csv(folder / "actual_weights.csv").drop(columns="date")
    assert np.allclose(weights.sum(axis=1), 1)
    phases = pd.read_csv(folder / "phases.csv")
    assert np.prod(1 + phases.phase_return) == pytest.approx(run["curve"][-1]["nav"])
    steps = pd.read_csv(folder / "screening_steps.csv")
    assert (steps.after_dedup == steps.eligible).all()
    assert (folder / "returns.png").stat().st_size > 1000
