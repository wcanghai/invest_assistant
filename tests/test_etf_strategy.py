"""Behavioral coverage for point-in-time research and manual account ledgers."""

import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from etf_strategy.backtest import backtest, metrics
from etf_strategy.config import load_config
from etf_strategy.data import Dataset
from etf_strategy.collect import collect
from etf_strategy.manual import (
    import_account_events, import_fills, import_snapshot, reconcile, trade_plan,
)
from etf_strategy.storage import import_records, migrate
from etf_strategy.strategies import covariance, equal_risk, target


@pytest.fixture
def market(tmp_path):
    # Build independent known-price assets across four sleeves and three pure styles.
    path = tmp_path / "source.db"
    schema = __import__("pathlib").Path("security_pool/schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
    migrate(path)
    dates = pd.bdate_range("2020-01-01", periods=380).strftime("%Y-%m-%d").tolist()
    classifications = []
    with sqlite3.connect(path) as conn:
        for number, kind in enumerate(("equity", "bond", "gold", "commodity",
                                       "equity", "equity", "equity")):
            code = f"{number:06d}.SH"
            conn.execute("INSERT INTO etf_master(etf_code,etf_name,exchange,list_date,"
                         "underlying_code,trade_unit,first_seen_date,created_at,updated_at) "
                         "VALUES(?,?,'SH',?,'INDEX',100,?,?,?)",
                         (code, code, dates[0], dates[0], dates[0], dates[0]))
            for i, day in enumerate(dates):
                price = 10 * np.exp(0.001 * i + 0.002 * np.sin(i / (3 + number)))
                conn.execute("INSERT INTO etf_daily(etf_code,trade_date,open,high,low,close,"
                             "volume,amount_10k,is_trading,forward_factor,updated_at) "
                             "VALUES(?,?,?,?,?,?,1000000,10000,1,1,?)",
                             (code, day, price, price * 1.01, price * .99, price, day))
            classifications.append({
                "etf_code": code, "valid_from": dates[0], "valid_to": None,
                "known_at": dates[0], "asset_class": kind, "market": "CN",
                "exposure_group": code, "pool": "base" if number < 4 else "factor",
                "factor_style": ("value", "quality", "low_vol")[number - 4]
                if number >= 4 else None,
                "source": "synthetic_test_fixture", "verified": 1, "historical_verified": 1})
    import_records(path, "etf_classification", classifications)
    import_records(path, "etf_calendar", [{"trade_date": d, "source": "fixture", "verified": 1}
                                           for d in dates])
    return path, dates


def test_universe_future_isolation_and_dedup(market):
    # Future bars cannot alter a past pool, and same-index representatives are deterministic.
    path, dates = market
    cfg = load_config()
    before = Dataset(path, cfg).universe(dates[300])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET close=999,amount_10k=1 "
                     "WHERE trade_date>?", (dates[300],))
        conn.execute("UPDATE etf_classification SET exposure_group='000000.SH' "
                     "WHERE etf_code='000001.SH'")
    after = Dataset(path, cfg).universe(dates[300])
    assert before.eligible.sum() == 4
    assert after.eligible.sum() == 3
    assert after.set_index("etf_code").at["000001.SH", "reasons"] == ["duplicate_exposure"]
    assert before.adv20_10k.equals(after.adv20_10k)


def test_top3_cash_and_trend_cash(market):
    # A single trend-qualified asset must not absorb unavailable slots or sleeves.
    path, dates = market
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_classification SET verified=0 WHERE etf_code!='000000.SH'")
    data = Dataset(path, load_config())
    result = target(data, dates[300], "dual_momentum")
    assert sum(result["weights"].values()) == pytest.approx(1 / 3)
    assert target(data, dates[300], "trend")["cash_weight"] == .75


def test_factor_missing_style_and_liquidity(market):
    # Never replace missing pure quality with another factor or waive liquidity thresholds.
    path, dates = market
    data = Dataset(path, load_config())
    assert target(data, dates[300], "factor_rotation")["status"] == "ok"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET amount_10k=10 WHERE etf_code='000005.SH'")
    data = Dataset(path, load_config())
    assert target(data, dates[300], "factor_rotation")["status"] == "not_eligible"


def test_covariance_and_solver(market):
    # Correlation-aware risk contributions converge; short or degenerate samples fail closed.
    weights, details = equal_risk(np.array([[.04, .005], [.005, .01]]))
    assert weights[0] == pytest.approx(1 / 3, abs=1e-6)
    assert details["residual"] < 1e-8
    with pytest.raises(ValueError):
        covariance(pd.DataFrame({"a": [0.1] * 50}), load_config())
    with pytest.raises(ValueError):
        equal_risk(np.zeros((2, 2)))
    path, dates = market
    result = target(Dataset(path, load_config()), dates[300], "risk_parity")
    assert result["status"] == "ok"
    assert sum(result["weights"].values()) == pytest.approx(1)


def test_validated_gate_and_unknown_calendar(market):
    # Nonempty factors alone must not certify historical performance.
    path, dates = market
    cfg = load_config(overrides={"price_mode": "validated"})
    data = Dataset(path, cfg)
    assert data.universe(dates[300]).eligible.sum() == 0
    assert data.quality(dates[300], ["000000.SH"]) == "exploratory"
    assert target(data, dates[300], "trend")["status"] == "blocked"
    assert trade_plan(data, target(data, dates[300], "trend"))["orders"] == []


def test_unknown_liquidity_not_zero_and_new_listing(market):
    # Unknown missing sessions and future listing dates fail separate auditable gates.
    path, dates = market
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET amount_10k=NULL WHERE etf_code='000000.SH' "
                     "AND trade_date=?", (dates[295],))
        conn.execute("UPDATE etf_master SET list_date=? WHERE etf_code='000001.SH'",
                     (dates[310],))
    frame = Dataset(path, load_config()).universe(dates[300]).set_index("etf_code")
    assert "unknown_liquidity" in frame.at["000000.SH", "reasons"]
    assert "not_listed_or_unknown_listing" in frame.at["000001.SH", "reasons"]


def test_next_day_and_costs(market):
    # A frozen close signal fills only next session and buys above the raw open.
    path, dates = market
    data = Dataset(path, load_config())
    decision = {"signal_date": dates[300], "weights": {"000000.SH": 1},
                "status": "ok", "quality_level": "exploratory"}
    result = backtest(data, "trend", dates[300], dates[305], signals={dates[300]: decision})
    trade = result["trades"][0]
    assert trade["date"] == dates[301]
    assert trade["price"] > data.bars["000000.SH"].at[dates[301], "open"]
    assert all(r["cash"] >= -1e-10 for r in result["curve"])


def test_dividend_receivable_and_split(market):
    # Entitlements survive sales and payment does not double-count ex-date receivables.
    path, dates = market
    code = "000000.SH"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET open=10,high=10.1,low=9.9,close=10 "
                     "WHERE etf_code=?", (code,))
        conn.execute("UPDATE etf_daily SET open=4.5,high=4.6,low=4.4,close=4.5, "
                     "forward_factor=10.0/4.5 "
                     "WHERE etf_code=? AND trade_date>=?", (code, dates[303]))
    import_records(path, "etf_action", [{
        "event_id": "distribution", "etf_code": code, "record_date": dates[302],
        "ex_date": dates[303], "pay_date": dates[305], "cash_per_share": 1,
        "share_multiplier": 2, "known_at": dates[300], "source": "fixture", "verified": 1}])
    cfg = load_config(overrides={"fee_bps": 0, "slippage_bps": 0})
    data = Dataset(path, cfg)
    decision = {"signal_date": dates[300], "weights": {code: 1},
                "status": "ok", "quality_level": "exploratory"}
    result = backtest(data, "trend", dates[300], dates[306], signals={dates[300]: decision})
    curve = {r["date"]: r for r in result["curve"]}
    assert curve[dates[303]]["nav"] == pytest.approx(1)
    assert curve[dates[303]]["receivables"] == pytest.approx(.1)
    assert curve[dates[305]]["receivables"] == 0
    assert curve[dates[305]]["nav"] == pytest.approx(1)
    assert curve[dates[305]]["cash"] == pytest.approx(.1)


def test_account_capacity_partial_and_lots(market):
    # Explicit account capital enables lot rounding and capacity-limited partial fills.
    path, dates = market
    data = Dataset(path, load_config())
    account = {"initial_cash": 100000, "fee_bps": 3, "minimum_fee": 5,
               "slippage_bps": 5, "lot_sizes": {"000000.SH": 100},
               "max_adv_participation": .0001}
    decision = {"signal_date": dates[300], "weights": {"000000.SH": 1},
                "status": "ok", "quality_level": "exploratory"}
    result = backtest(data, "trend", dates[300], dates[305], account,
                      {dates[300]: decision})
    trade = result["trades"][0]
    assert trade["quantity"] % 100 == 0
    assert trade["status"] == "partial_expired"
    assert trade["fee"] >= 5


def test_fill_idempotence_atomicity_reconcile(tmp_path):
    # Duplicate fills are no-ops, conflicting duplicates and insolvent batches roll back.
    path = tmp_path / "ledger.db"
    snapshot = {"account_id": "a", "as_of": "2026-09-10T15:00:00",
                "cash": 10000, "holdings": {}}
    import_snapshot(path, snapshot)
    fill = {"fill_id": "f1", "timestamp": "2026-09-11T10:00:00", "code": "000001.SH",
            "side": "buy", "quantity": 100, "price": 10, "fee": 5}
    assert import_fills(path, "a", [fill])["imported"] == 1
    assert import_fills(path, "a", [fill])["imported"] == 0
    with pytest.raises(ValueError):
        import_fills(path, "a", [{**fill, "price": 11}])
    with pytest.raises(ValueError):
        import_fills(path, "a", [{**fill, "fill_id": "f2", "quantity": 100000}])
    actual = {**snapshot, "cash": 8995, "holdings": {"000001.SH": 100}}
    assert reconcile(path, actual, {"000001.SH": 10})["matched"]


def test_drawdown_and_reproducibility(market):
    # Preserve ongoing drawdown duration and determinism on identical inputs.
    curve = [{"date": d, "nav": n, "cash_weight": 0, "turnover": 0}
             for d, n in zip(("2020-01-01", "2020-01-02", "2020-01-03"), (1, .8, .9))]
    assert metrics(curve)["ongoing_drawdown_sessions"] == 2
    path, dates = market
    data = Dataset(path, load_config())
    a = target(data, dates[300], "dual_momentum")
    b = target(data, dates[300], "dual_momentum")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_point_in_time_future_classification(market):
    # Future knowledge cannot be used even when the classification claims an earlier valid date.
    path, dates = market
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_classification SET known_at=?", (dates[310],))
    data = Dataset(path, load_config(overrides={"universe_mode": "point_in_time"}))
    assert data.universe(dates[300]).eligible.sum() == 0


def test_manual_plan_with_verified_inputs(market):
    # Orders require a validated scheduled signal, next session and explicit account limits.
    path, dates = market
    import_records(path, "etf_universe_validation", [{
        "start_date": dates[0], "end_date": dates[-1], "source": "fixture",
        "validated_at": dates[-1], "terminated_products_complete": 1,
        "historical_mappings_complete": 1}])
    codes = [f"{i:06d}.SH" for i in range(4)]
    for code in codes:
        import_records(path, "etf_validation", [{
            "etf_code": code, "start_date": dates[0], "end_date": dates[-1],
            "validated_at": dates[-1], "source": "fixture_independent_review",
            "factor_direction": "multiply", "actions_complete": 1, "identity_complete": 1,
            "trading_rules_verified": 1}])
        import_records(path, "etf_identity", [{
            "etf_code": code, "list_date": dates[0], "end_date": None,
            "known_at": dates[0], "source": "fixture", "verified": 1}])
        import_records(path, "etf_rule", [{
            "etf_code": code, "valid_from": dates[0], "valid_to": None,
            "known_at": dates[0], "lot_size": 100, "price_tick": .001,
            "is_t0": 0, "source": "fixture", "verified": 1}])
    cfg = load_config(overrides={
        "universe_mode": "point_in_time", "price_mode": "validated",
    })
    data = Dataset(path, cfg)
    day, execution_day = "2021-02-26", "2021-03-01"
    decision = target(data, day, "trend")
    assert decision["quality_level"] == "validated_research"
    snapshot = {"account_id": "test", "as_of": execution_day + "T09:30:00",
                "cash": 100000, "holdings": {}, "sellable": {}}
    execution = {"execution_date": execution_day, "fee_bps": 3, "minimum_fee": 5,
                 "max_adv_participation": .01, "max_asset_weight": .5, "class_caps": {},
                 "quotes": {c: {"date": execution_day, "price": 10, "tradable": True}
                            for c in codes}}
    plan = trade_plan(data, decision, snapshot, execution)
    assert plan["status"] == "manual_execution_ready"
    assert len(plan["orders"]) == 4
    assert plan["estimated_cash"] >= 0
    with pytest.raises(ValueError):
        trade_plan(data, decision, snapshot, {**execution, "execution_date": "2021-03-02"})
    with pytest.raises(ValueError):
        trade_plan(data, decision, snapshot, {**execution, "max_asset_weight": .1})


def test_manual_cash_events_idempotent(tmp_path):
    # Cash dividends and splits reconcile actual broker balances without fabricating trades.
    path = tmp_path / "events.db"
    snapshot = {"account_id": "x", "as_of": "2026-09-01T15:00:00",
                "cash": 100, "holdings": {"A": 100}}
    import_snapshot(path, snapshot)
    event = {"event_id": "e", "timestamp": "2026-09-02T10:00:00", "kind": "dividend_paid",
             "code": "A", "cash_amount": 10, "share_multiplier": 1, "source": "broker"}
    assert import_account_events(path, "x", [event])["cash"] == 110
    assert import_account_events(path, "x", [event])["imported"] == 0
    assert reconcile(path, {**snapshot, "cash": 110})["matched"]


def test_suspension_preserves_holding_and_rejects_same_price_session(market):
    # Missing exits retain the asset and stale valuation; they cannot become imaginary cash.
    path, dates = market
    code = "000000.SH"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET open=NULL,close=NULL,is_trading=0,volume=0 "
                     "WHERE etf_code=? AND trade_date=?", (code, dates[303]))
    data = Dataset(path, load_config())
    buy = {"signal_date": dates[300], "weights": {code: .5}, "status": "ok",
           "quality_level": "exploratory"}
    sell = {**buy, "signal_date": dates[302], "weights": {}}
    result = backtest(data, "trend", dates[300], dates[305],
                      signals={dates[300]: buy, dates[302]: sell})
    assert len(result["trades"]) == 1
    assert result["curve"][3]["stale_codes"] == [code]
    assert result["curve"][-1]["holdings"][code] > 0


def test_risk_limit_does_not_redistribute(market):
    # Caps release money into cash and never lever the remaining holdings.
    path, dates = market
    cfg = load_config(overrides={"max_asset_weight": .10, "target_volatility": .01})
    result = target(Dataset(path, cfg), dates[300], "dual_momentum")
    assert all(w <= .1 for w in result["weights"].values())
    assert result["cash_weight"] >= .7 - 1e-10


def test_partial_calendar_cannot_erase_history(market):
    # An incomplete calendar cannot reduce lookback denominators or promote quality.
    path, dates = market
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM etf_calendar WHERE trade_date<?", (dates[290],))
    data = Dataset(path, load_config())
    assert len(data.dates) == len(dates)
    assert not data.calendar_verified
    assert data.universe(dates[300]).eligible.sum() == 4


def test_collect_fills_missing_only_and_retains_raw_events(market, tmp_path):
    # Vendor supplements fill null OHLC cells without revising prices or certifying empty events.
    path, dates = market
    code = "000000.SH"
    with sqlite3.connect(path) as conn:
        old = conn.execute("SELECT close FROM etf_daily WHERE etf_code=? AND trade_date=?",
                           (code, dates[300])).fetchone()[0]
        conn.execute("UPDATE etf_daily SET close=NULL WHERE etf_code=? AND trade_date=?",
                     (code, dates[301]))

    class Vendor:
        def get_market_data(self, *args):
            # Return deliberately different existing values to test non-overwriting backfill.
            fields = {"Open": 20, "High": 21, "Low": 19, "Close": 20,
                      "Volume": 1000, "Amount": 3000, "ForwardFactor": 2}
            return {k: pd.DataFrame({code: [v, v]}, index=dates[300:302])
                    for k, v in fields.items()}

        def get_divid_factors(self, *args):
            # Empty events represent missing evidence, not a completed validation.
            return pd.DataFrame()

        def get_trading_dates(self, *args):
            # Supply explicit dates in the vendor's compact format.
            return [d.replace("-", "") for d in dates[300:302]]

    result = collect(path, [code], dates[300], dates[301], tmp_path / "raw.json", Vendor())
    assert not result["errors"]
    with sqlite3.connect(path) as conn:
        first = conn.execute("SELECT close FROM etf_daily WHERE etf_code=? AND trade_date=?",
                             (code, dates[300])).fetchone()[0]
        second = conn.execute("SELECT close FROM etf_daily WHERE etf_code=? AND trade_date=?",
                              (code, dates[301])).fetchone()[0]
        assert first == old
        assert second == 20
        assert conn.execute("SELECT count(*) FROM etf_action").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM etf_validation").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM etf_daily_provenance").fetchone()[0] == 4


def test_additive_migration_preserves_old_prices(tmp_path):
    # Legacy ETF tables gain a nullable factor column without rewriting raw observations.
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE etf_daily(etf_code TEXT,trade_date TEXT,close REAL)")
        conn.execute("INSERT INTO etf_daily VALUES('A','2020-01-01',12.3)")
    migrate(path)
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT close,forward_factor FROM etf_daily").fetchone() == (12.3, None)
        assert len(list(conn.execute("PRAGMA table_info(etf_daily)"))) == 4


def test_future_prices_do_not_change_past_signal(market):
    # Signals and pool membership are identical after arbitrary future OHLC and liquidity edits.
    path, dates = market
    cfg = load_config()
    before = target(Dataset(path, cfg), dates[300], "dual_momentum")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_daily SET close=999,amount_10k=1 "
                     "WHERE trade_date>?", (dates[300],))
    after = target(Dataset(path, cfg), dates[300], "dual_momentum")
    assert before == after


def test_report_phases_cover_and_reconcile_every_session(market):
    # Reporting phases neither omit nor double-count the boundary-day returns and costs.
    from etf_strategy.ten_year import phase_ledger

    path, dates = market
    data = Dataset(path, load_config())
    buy = {"signal_date": dates[300], "weights": {"000000.SH": .8},
           "cash_weight": .2, "status": "ok", "quality_level": "exploratory", "universe": []}
    cash = {**buy, "signal_date": dates[305], "weights": {}, "cash_weight": 1}
    result = backtest(data, "trend", dates[299], dates[310],
                      signals={dates[300]: buy, dates[305]: cash})
    phases, contributions = phase_ledger(result, data)
    assert phases.sessions.sum() == len(result["curve"])
    assert (1 + phases.phase_return).prod() == pytest.approx(result["curve"][-1]["nav"])
    assert phases.reconciliation_residual.abs().max() < 1e-9
    grouped = contributions.groupby("phase").phase_return_contribution.sum()
    for row in phases.itertuples():
        assert grouped.get(row.phase, 0) == pytest.approx(row.phase_return)
    assert phases.iloc[1].start == dates[301]
    assert phases.iloc[1].end == dates[305]


def test_unqualified_report_does_not_claim_strategy_returns(market, tmp_path):
    # Inactive factor policy reports no performance instead of marketing the flat cash ledger.
    from etf_strategy.ten_year import evaluation, phase_ledger

    path, dates = market
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE etf_classification SET verified=0 WHERE pool='factor'")
    data = Dataset(path, load_config())
    result = backtest(data, "factor_rotation", dates[290], dates[350])
    phases, contributions = phase_ledger(result, data)
    assert result["first_active_signal"] is None
    assert phases.phase_return.eq(0).all()
    assert contributions.empty
    notes = evaluation(result, {}, phases, {})
    assert "不能据此报告" in notes[0]
