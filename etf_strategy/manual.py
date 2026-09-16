"""Local manual-order plans and transactional actual-fill reconciliation."""

import json
import math

from .config import CLASSES, digest
from .storage import connect


def init_ledger(conn):
    # Keep paper/manual accounts isolated from the market-data database.
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS manual_account (
      account_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, cash REAL NOT NULL,
      holdings_json TEXT NOT NULL, initial_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS manual_fill (
      account_id TEXT NOT NULL, fill_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      PRIMARY KEY(account_id,fill_id));
    CREATE TABLE IF NOT EXISTS manual_plan (
      plan_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS manual_event (
      account_id TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      PRIMARY KEY(account_id,event_id));
    """)


def validate_snapshot(snapshot):
    # Validate real account snapshots without inventing cash or positions.
    required = {"account_id", "as_of", "cash", "holdings"}
    if not required.issubset(snapshot):
        raise ValueError(f"Snapshot requires {sorted(required)}")
    if not math.isfinite(snapshot["cash"]) or snapshot["cash"] < 0:
        raise ValueError("Invalid cash")
    for value in snapshot["holdings"].values():
        if not math.isfinite(value) or value < 0:
            raise ValueError("Invalid holding")


def import_snapshot(path, snapshot):
    # Initialize once; subsequent broker snapshots are reconciled, never silently overwrite fills.
    validate_snapshot(snapshot)
    with connect(path) as conn:
        init_ledger(conn)
        old = conn.execute("SELECT initial_json FROM manual_account WHERE account_id=?",
                           (snapshot["account_id"],)).fetchone()
        payload = json.dumps(snapshot, sort_keys=True)
        if old:
            if old[0] != payload:
                raise ValueError("Account exists; use reconcile for later broker snapshots")
            return "unchanged"
        conn.execute("INSERT INTO manual_account VALUES(?,?,?,?,?)",
                     (snapshot["account_id"], snapshot["as_of"], snapshot["cash"],
                      json.dumps(snapshot["holdings"]), payload))
    return "initialized"


def import_fills(path, account_id, fills):
    # Apply actual fills atomically and reject conflicting duplicates, shorts or negative cash.
    with connect(path) as conn:
        init_ledger(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM manual_account WHERE account_id=?",
                           (account_id,)).fetchone()
        if not row:
            raise ValueError("Import initial account snapshot first")
        cash, holdings, as_of = row["cash"], json.loads(row["holdings_json"]), row["as_of"]
        imported = 0
        for fill in sorted(fills, key=lambda f: (f["timestamp"], f["fill_id"])):
            required = {"fill_id", "timestamp", "code", "side", "quantity", "price", "fee"}
            if set(fill) != required:
                raise ValueError(f"Fill fields must be {sorted(required)}")
            payload = json.dumps(fill, sort_keys=True, allow_nan=False)
            old = conn.execute("SELECT payload_json FROM manual_fill "
                               "WHERE account_id=? AND fill_id=?",
                               (account_id, fill["fill_id"])).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError("Conflicting duplicate fill")
                continue
            if fill["timestamp"] < as_of:
                raise ValueError("Out-of-order fill; reconcile/rebuild required")
            quantity, price, cost = fill["quantity"], fill["price"], fill["fee"]
            if (not all(math.isfinite(v) for v in (quantity, price, cost))
                    or quantity <= 0 or price <= 0 or cost < 0):
                raise ValueError("Invalid fill numbers")
            code = fill["code"]
            if fill["side"] == "buy":
                cash -= quantity * price + cost
                holdings[code] = holdings.get(code, 0) + quantity
            elif fill["side"] == "sell":
                cash += quantity * price - cost
                holdings[code] = holdings.get(code, 0) - quantity
            else:
                raise ValueError("Fill side must be buy/sell")
            if cash < -1e-7 or holdings[code] < -1e-8:
                raise ValueError("Fill would create negative cash or short position")
            conn.execute("INSERT INTO manual_fill VALUES(?,?,?)",
                         (account_id, fill["fill_id"], payload))
            as_of = fill["timestamp"]
            imported += 1
        conn.execute("UPDATE manual_account SET cash=?,holdings_json=?,as_of=? "
                     "WHERE account_id=?", (cash, json.dumps(holdings), as_of, account_id))
    return {"imported": imported, "cash": cash, "holdings": holdings, "as_of": as_of}


def import_account_events(path, account_id, events):
    # Apply broker-confirmed distributions, splits and transfers with idempotent evidence IDs.
    with connect(path) as conn:
        init_ledger(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM manual_account WHERE account_id=?",
                           (account_id,)).fetchone()
        if not row:
            raise ValueError("Unknown account")
        cash, holdings, as_of = row["cash"], json.loads(row["holdings_json"]), row["as_of"]
        count = 0
        for event in sorted(events, key=lambda e: (e["timestamp"], e["event_id"])):
            required = {"event_id", "timestamp", "kind", "code", "cash_amount",
                        "share_multiplier", "source"}
            if set(event) != required or not event["source"]:
                raise ValueError(f"Account event requires {sorted(required)}")
            payload = json.dumps(event, sort_keys=True, allow_nan=False)
            old = conn.execute("SELECT payload_json FROM manual_event "
                               "WHERE account_id=? AND event_id=?",
                               (account_id, event["event_id"])).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError("Conflicting duplicate account event")
                continue
            if event["timestamp"] < as_of:
                raise ValueError("Out-of-order account event")
            amount, multiplier = event["cash_amount"], event["share_multiplier"]
            if not math.isfinite(amount) or not math.isfinite(multiplier) or multiplier <= 0:
                raise ValueError("Invalid account event amounts")
            if event["kind"] in ("dividend_paid", "cash_transfer"):
                if multiplier != 1 or (event["kind"] == "dividend_paid" and amount < 0):
                    raise ValueError("Invalid cash event")
                cash += amount
            elif event["kind"] == "split" and amount == 0:
                if event["code"] not in holdings:
                    raise ValueError("Split security is not held")
                holdings[event["code"]] *= multiplier
            else:
                raise ValueError("Unknown account event kind")
            if cash < 0:
                raise ValueError("Event would make cash negative")
            conn.execute("INSERT INTO manual_event VALUES(?,?,?)",
                         (account_id, event["event_id"], payload))
            as_of = event["timestamp"]
            count += 1
        conn.execute("UPDATE manual_account SET cash=?,holdings_json=?,as_of=? "
                     "WHERE account_id=?", (cash, json.dumps(holdings), as_of, account_id))
    return {"imported": count, "cash": cash, "holdings": holdings}


def reconcile(path, snapshot, prices=None):
    # Compare broker balances with the local ledger and report every nonzero difference.
    validate_snapshot(snapshot)
    with connect(path) as conn:
        init_ledger(conn)
        row = conn.execute("SELECT * FROM manual_account WHERE account_id=?",
                           (snapshot["account_id"],)).fetchone()
        if not row:
            raise ValueError("Unknown account")
    holdings = json.loads(row["holdings_json"])
    differences = {c: snapshot["holdings"].get(c, 0) - holdings.get(c, 0)
                   for c in set(holdings) | set(snapshot["holdings"])}
    cash_diff = snapshot["cash"] - row["cash"]
    result = {"cash_difference": cash_diff, "holding_differences": differences,
              "matched": abs(cash_diff) < 0.01 and all(abs(v) < 1e-8
                                                        for v in differences.values())}
    if prices is not None:
        if set(differences) - set(prices):
            raise ValueError("Missing valuation prices")
        result["equity_difference"] = cash_diff + sum(
            differences[c] * prices[c] for c in differences)
    return result


def trade_plan(dataset, decision, snapshot=None, execution=None):
    # Emit weights only until explicit account inputs and security evidence pass all gates.
    base = {"signal_date": decision["signal_date"], "weights": decision["weights"],
            "cash_weight": decision["cash_weight"], "status": "weights_only", "orders": []}
    if snapshot is None or execution is None:
        base["reason"] = "account_and_execution_settings_required"
        return base
    validate_snapshot(snapshot)
    if "sellable" not in snapshot:
        raise ValueError("Broker-confirmed sellable quantities required")
    required = {"execution_date", "fee_bps", "minimum_fee", "max_adv_participation",
                "quotes", "max_asset_weight", "class_caps"}
    if not required.issubset(execution):
        raise ValueError(f"Execution settings required: {sorted(required)}")
    if set(execution["class_caps"]) - set(CLASSES):
        raise ValueError("Unknown account asset class")
    if decision["status"] != "ok" or decision["quality_level"] != "validated_research":
        raise ValueError("Validated signal required for manual quantity orders")
    from .backtest import rebalance_days

    if decision["signal_date"] not in rebalance_days(dataset, decision["strategy"]):
        raise ValueError("Not a scheduled rebalance signal")
    day = execution["execution_date"]
    following = [d for d in dataset.all_dates if d > decision["signal_date"]]
    if not following or following[0] != day or snapshot["as_of"][:10] != day:
        raise ValueError("Signal expired or next exchange session is not verified")
    if not dataset.actions.empty:
        future = dataset.actions
        relevant = future[future.etf_code.isin(set(decision["weights"]) |
                                               set(snapshot["holdings"]))
                          & future.ex_date.gt(decision["signal_date"])
                          & future.ex_date.le(day)]
        if not relevant.empty:
            raise ValueError("Corporate action intervenes; reconcile adjusted holdings first")
    quotes = execution["quotes"]
    codes = sorted(set(decision["weights"]) | set(snapshot["holdings"]))
    rules = {}
    for code in codes:
        evidence = dataset.rules
        if evidence.empty:
            raise ValueError("Trading rules missing")
        matches = evidence[evidence.etf_code.eq(code) & evidence.valid_from.le(day)
                           & (evidence.valid_to.isna() | evidence.valid_to.ge(day))
                           & evidence.known_at.str[:10].le(day) & evidence.verified.eq(1)]
        if len(matches) != 1:
            raise ValueError(f"Unverified or ambiguous rules: {code}")
        rules[code] = matches.iloc[0]
        quote = quotes.get(code, {})
        if (quote.get("date") != day or not quote.get("tradable")
                or not math.isfinite(quote.get("price", float("nan")))
                or quote.get("price", 0) <= 0):
            raise ValueError(f"Fresh explicit tradable quote required: {code}")
    fee, minimum = execution["fee_bps"] / 10000, execution["minimum_fee"]
    cap = execution["max_adv_participation"]
    asset_cap = execution["max_asset_weight"]
    if (not 0 <= fee < 1 or not math.isfinite(minimum) or minimum < 0
            or not 0 < cap <= 1 or not 0 < asset_cap <= 1):
        raise ValueError("Invalid execution risk/cost parameters")
    if any(w > asset_cap for w in decision["weights"].values()):
        raise ValueError("Target violates account asset cap")
    mapping = dataset.classification(decision["signal_date"]).set_index("etf_code")
    for category, limit in execution["class_caps"].items():
        if not 0 < limit <= 1:
            raise ValueError("Invalid class cap")
        total = sum(w for c, w in decision["weights"].items()
                    if mapping.at[c, "asset_class"] == category)
        if total > limit:
            raise ValueError("Target violates account class cap")
    equity = snapshot["cash"] + sum(q * quotes[c]["price"]
                                     for c, q in snapshot["holdings"].items())
    cash, orders = snapshot["cash"], []
    for buying in (False, True):
        for code in codes:
            price, rule = quotes[code]["price"], rules[code]
            lot, tick = int(rule.lot_size), float(rule.price_tick)
            desired = math.floor(equity * decision["weights"].get(code, 0) / price / lot) * lot
            delta = desired - snapshot["holdings"].get(code, 0)
            if delta == 0 or (delta > 0) != buying:
                continue
            price = (math.ceil(price / tick) if buying else math.floor(price / tick)) * tick
            history = dataset.bars[code].loc[:decision["signal_date"]].tail(20)
            if len(history) < 20 or history.amount_10k.isna().any():
                raise ValueError("Insufficient account capacity evidence")
            capacity = history.amount_10k.mean() * 10000 * cap / price
            quantity = min(abs(delta), capacity)
            if not buying:
                available = snapshot["sellable"].get(code, 0)
                if not math.isfinite(available) or available < 0:
                    raise ValueError("Invalid sellable quantity")
                quantity = min(quantity, available)
            if buying:
                quantity = min(quantity, max(0, (cash - minimum) / (price * (1 + fee))))
            quantity = math.floor(quantity / lot) * lot
            if not quantity:
                continue
            amount = quantity * price
            cost = max(minimum, amount * fee)
            if not buying and amount <= cost:
                continue
            cash += -amount - cost if buying else amount - cost
            orders.append({"code": code, "side": "buy" if buying else "sell",
                           "quantity": quantity, "limit_price": round(price, 8),
                           "estimated_fee": cost, "depends_on_sell_fills": buying})
    base.update(status="manual_execution_ready", orders=orders, expires_at=f"{day}T15:00:00+08:00",
                estimated_cash=cash, account_id=snapshot["account_id"],
                warning="Execute sells first; refresh cash after actual fills before buying")
    base["plan_id"] = digest(base)
    return base
