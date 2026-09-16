"""Event-driven fractional/account ledger with raw-price execution and cash dividends."""

import math

import numpy as np
import pandas as pd

from .strategies import target
from .events import effective_date


def metrics(curve):
    # Measure calendar-year CAGR and drawdowns including an unresolved final episode.
    if not curve:
        return {"status": "no_history"}
    frame = pd.DataFrame(curve)
    nav = frame.nav.astype(float)
    returns = nav.pct_change(fill_method=None)
    returns.iloc[0] = nav.iloc[0] - 1
    peak = nav.cummax().clip(lower=1)
    drawdown = nav / peak - 1
    elapsed = max(1, (pd.Timestamp(frame.date.iloc[-1])
                      - pd.Timestamp(frame.date.iloc[0])).days + 1)
    cagr = float(nav.iloc[-1] ** (365.25 / elapsed) - 1)
    maximum = float(drawdown.min())
    longest, current = 0, 0
    for value in drawdown:
        current = current + 1 if value < -1e-12 else 0
        longest = max(longest, current)
    annual = returns.groupby(frame.date.str[:4]).apply(lambda x: float((1 + x).prod() - 1))
    return {"start": frame.date.iloc[0], "end": frame.date.iloc[-1], "cagr": cagr,
            "max_drawdown": maximum,
            "sharpe_zero_rf": float(returns.mean() / returns.std() * np.sqrt(252))
            if returns.std() > 0 else None,
            "calmar": cagr / abs(maximum) if maximum < 0 else None,
            "longest_drawdown_sessions": longest, "ongoing_drawdown_sessions": current,
            "unrecovered": current > 0, "annual_returns": annual.to_dict(),
            "one_way_turnover": float(frame.turnover.sum() / 2),
            "average_cash_weight": float(frame.cash_weight.mean())}


def rebalance_days(dataset, strategy):
    # Require an observed/verified next session to identify a genuine period boundary.
    dates = dataset.all_dates
    ends = set()
    for day, following in zip(dates, dates[1:]):
        if day[:7] != following[:7]:
            if strategy != "strategic" or int(day[5:7]) in (3, 6, 9, 12):
                ends.add(day)
    return ends


def validate_account(account):
    # Account backtests require explicit capital, fees, lot sizes and participation limits.
    if account is None:
        return
    required = {"initial_cash", "fee_bps", "minimum_fee", "slippage_bps", "lot_sizes",
                "max_adv_participation"}
    if not required.issubset(account):
        raise ValueError(f"Account settings required: {sorted(required)}")
    for key in required - {"lot_sizes"}:
        if not math.isfinite(account[key]) or account[key] < 0:
            raise ValueError(f"Invalid account {key}")
    if account["initial_cash"] <= 0 or not 0 < account["max_adv_participation"] <= 1:
        raise ValueError("Invalid account capital or capacity")
    if max(account["fee_bps"], account["slippage_bps"]) >= 10000:
        raise ValueError("Invalid account costs")
    if any(type(v) is not int or v <= 0 for v in account["lot_sizes"].values()):
        raise ValueError("Invalid lot sizes")


def backtest(dataset, strategy, start, end, account=None, signals=None):
    # Process entitlements, next-session orders, raw valuation and then closing signals.
    validate_account(account)
    cfg = dataset.config
    initial = account["initial_cash"] if account else 1.0
    cash, shares, last_prices = initial, {}, {}
    fee = (account or cfg)["fee_bps"] / 10000
    slip = (account or cfg)["slippage_bps"] / 10000
    minimum = account["minimum_fee"] if account else 0.0
    dates = [d for d in dataset.dates if start <= d <= end]
    schedule = rebalance_days(dataset, strategy)
    month_ends = rebalance_days(dataset, "trend")
    pending, previous, receivables = None, None, {}
    entitlements, curve, trades, signal_rows, warnings = {}, [], [], [], []
    actions = dataset.actions
    if not actions.empty:
        actions = actions[actions.verified.eq(1)].to_dict("records")
    else:
        actions = []
    active_start = None
    publication_issues, event_ledger = [], []
    due_date = None
    for day in dates:
        exposed = dict(shares)
        if pending is not None and day >= due_date:
            for code, weight in pending["weights"].items():
                exposed[code] = max(exposed.get(code, 0), weight)
        for code, quantity in exposed.items():
            if quantity <= 1e-12:
                continue
            if cfg["price_mode"] != "exploratory":
                evidence = dataset.validation
                covered = evidence[evidence.etf_code.eq(code) & evidence.actions_complete.eq(1)
                                   & evidence.start_date.le(day) & evidence.end_date.ge(day)]
                bar = dataset.bars[code].loc[day]
                if covered.empty or (bar.valid and not bar.forward_factor > 0):
                    publication_issues.append({"code": code, "date": day,
                                               "reason": "held_price_evidence_missing"})
            for issue in getattr(dataset, "action_issues", {}).get(code, []):
                if issue["date"] == day:
                    publication_issues.append({"code": code, **issue})
        # Entitlements are recorded after the record-date close, before ex-date trading.
        for action in actions:
            key, code = action["event_id"], action["etf_code"]
            if effective_date(action) == day:
                entitled = entitlements.get(key, 0)
                receivables[key] = entitled * action["cash_per_share"]
                shares[code] = shares.get(code, 0) * action["share_multiplier"]
                if code in last_prices:
                    last_prices[code] = ((last_prices[code] - action["cash_per_share"])
                                         / action["share_multiplier"])
                event_ledger.append({"date": day, "code": code, "event_id": key,
                                     "cash_entitlement": receivables[key],
                                     "share_multiplier": action["share_multiplier"]})
            if action["pay_date"] <= day and key in receivables:
                cash += receivables.pop(key)
        turnover = 0.0
        if pending is not None and day >= due_date:
            weights = pending["weights"]
            opens, permitted = {}, {}
            for code in sorted(set(shares) | set(weights)):
                bar = dataset.bars[code].loc[day]
                valid = (bar.valid and pd.notna(bar.open) and bar.open > 0
                         and bar.high >= bar.open >= bar.low > 0)
                # A one-price session cannot demonstrate executable market liquidity.
                if valid and bar.high == bar.low:
                    valid = False
                permitted[code] = valid
                opens[code] = float(bar.open) if valid else last_prices.get(code, 0)
            equity = cash + sum(receivables.values()) + sum(
                q * opens.get(c, last_prices.get(c, 0)) for c, q in shares.items())
            requests = []
            for code in sorted(set(shares) | set(weights)):
                price = opens[code]
                if not permitted[code] or price <= 0:
                    warnings.append({"date": day, "code": code, "reason": "not_executable"})
                    continue
                lot = account["lot_sizes"].get(code) if account else None
                if account and not lot:
                    raise ValueError(f"Missing account lot size: {code}")
                desired = equity * weights.get(code, 0) / price
                desired = math.floor(desired / lot) * lot if lot else desired
                delta = desired - shares.get(code, 0)
                requests.append((delta > 0, code, delta, lot))
            for buying, code, delta, lot in sorted(requests):
                if abs(delta) < 1e-12:
                    continue
                price = opens[code] * (1 + slip if buying else 1 - slip)
                quantity = abs(delta)
                if account:
                    history = dataset.bars[code].loc[:previous].tail(20)
                    adv = history.amount_10k.mean() * 10000
                    quantity = min(quantity, adv * account["max_adv_participation"] / price)
                if buying:
                    quantity = min(quantity, max(0, (cash - minimum) / (price * (1 + fee))))
                if lot:
                    quantity = math.floor(quantity / lot) * lot
                if quantity <= 1e-12:
                    continue
                amount = quantity * price
                cost = max(minimum, amount * fee)
                if buying:
                    cash -= amount + cost
                    shares[code] = shares.get(code, 0) + quantity
                else:
                    if amount <= cost:
                        continue
                    cash += amount - cost
                    shares[code] -= quantity
                turnover += amount / equity
                trades.append({"date": day, "signal_date": pending["signal_date"],
                               "code": code, "side": "buy" if buying else "sell",
                               "quantity": quantity, "price": price, "fee": cost,
                               "status": "filled" if quantity >= abs(delta) - 1e-10
                               else "partial_expired"})
            pending = None
        stale = []
        for code, quantity in shares.items():
            if quantity <= 1e-12:
                continue
            bar = dataset.bars[code].loc[day]
            if bar.valid:
                last_prices[code] = float(bar.close)
            else:
                stale.append(code)
        equity = cash + sum(receivables.values()) + sum(
            q * last_prices.get(c, 0) for c, q in shares.items())
        if cash < -1e-8 or equity <= 0:
            raise ValueError("Ledger insolvency")
        curve.append({"date": day, "nav": equity / initial, "cash": cash,
                      "receivables": sum(receivables.values()), "cash_weight": cash / equity,
                      "turnover": turnover, "stale_codes": stale,
                      "holding_values": {c: q * last_prices.get(c, 0)
                                         for c, q in shares.items() if q > 1e-12},
                      "holdings": {c: q for c, q in shares.items() if q > 1e-12}})
        for action in actions:
            if action["record_date"] == day:
                entitlements[action["event_id"]] = shares.get(action["etf_code"], 0)
        if signals is not None:
            decision = signals.get(day)
        else:
            scheduled = day in schedule or (strategy == "buy_hold" and day == dates[0])
            decision = target(dataset, day, strategy, holdings=shares) if scheduled else None
            if strategy == "buy_hold" and active_start and cfg["target_volatility"] is None:
                decision = None
        if decision is not None:
            signal_rows.append(decision)
            if decision["status"] == "ok":
                pending = decision
                following = dataset.dates[dataset.dates > day]
                delay = cfg.get("execution_delay", 0)
                due_date = following[delay] if len(following) > delay else "9999-12-31"
                if active_start is None and decision["weights"]:
                    active_start = day
            else:
                warnings.append({"date": day, "reason": decision.get("reason")})
        if strategy == "strategic" and day in month_ends and shares:
            universe = dataset.universe(day)
            good = set(universe.loc[universe.eligible, "etf_code"])
            for code in shares:
                if shares[code] > 0 and code not in good:
                    warnings.append({"date": day, "code": code,
                                     "reason": "monthly_holding_quality_alert"})
        previous = day
    measured = metrics(curve)
    if curve:
        changes = pd.Series([r["nav"] for r in curve]).pct_change(fill_method=None)
        measured["realized_volatility"] = float(changes.std() * np.sqrt(252))
    if publication_issues:
        measured = {"status": "publication_blocked", "issues": publication_issues}
    status = "evaluated" if active_start else "no_eligible_active_period"
    first_execution = next((t["date"] for t in trades if t["side"] == "buy"), None)
    if active_start and first_execution is None:
        status = "no_executable_active_period"
    if publication_issues:
        status = "publication_blocked"
    return {"strategy": strategy, "metadata": dataset.metadata,
            "curve": [] if publication_issues else curve,
            "trades": trades, "signals": signal_rows, "warnings": warnings,
            "metrics": measured, "first_active_signal": active_start,
            "first_execution": first_execution,
            "publication_issues": publication_issues, "events": event_ledger,
            "quality_level": "exploratory" if not signal_rows else
            min((s["quality_level"] for s in signal_rows),
                key=lambda x: x != "exploratory"),
            "execution_model": "account" if account else "fractional_raw_price",
            "status": status}
