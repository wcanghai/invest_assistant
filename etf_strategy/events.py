"""Corporate-action alignment and publication barriers independent of research quality labels."""

import pandas as pd


def effective_date(event):
    # Old event imports remain compatible; split bookkeeping uses the actual registration date.
    return event.get("share_change_date") or event["ex_date"]


def event_checks(bar, actions, end):
    # Match factor jumps to the first valid quotation after the legal event, including suspensions.
    valid = bar.loc[:end]
    valid = valid[valid.valid]
    if valid.empty:
        return [], []
    events = actions.to_dict("records") if not actions.empty else []
    events = [e for e in events if e["verified"] == 1 and effective_date(e) <= end]
    explained, checks = set(), []
    for event in events:
        eligible = valid.index[valid.index >= effective_date(event)]
        if not len(eligible):
            continue
        day = eligible[0]
        i = valid.index.get_loc(day)
        if i == 0:
            continue
        prior = valid.close.iloc[i - 1]
        theoretical = (prior - event["cash_per_share"]) / event["share_multiplier"]
        ratio = valid.forward_factor.iloc[i] / valid.forward_factor.iloc[i - 1]
        consistent = theoretical > 0 and pd.notna(ratio)
        consistent = consistent and abs(ratio * theoretical / prior - 1) <= .001
        checks.append({"event_id": event["event_id"], "date": day,
                       "status": "consistent" if consistent else "mismatch"})
        if consistent:
            explained.add(day)
    ratios = valid.forward_factor / valid.forward_factor.shift(1)
    changes = set(ratios.index[(ratios - 1).abs() > 1e-7])
    raw = valid.close / valid.close.shift(1) - 1
    suspicious = set(raw.index[raw.abs() >= .40])
    issues = [{"date": d, "reason": "unexplained_factor_or_large_price_jump"}
              for d in sorted((changes | suspicious) - explained)]
    issues += [c for c in checks if c["status"] != "consistent"]
    return checks, issues
