"""Stateful Top5 rotation with auditable retention and exposure constraints."""

import pandas as pd


def select_rotation(dataset, day, selected, feat, holdings):
    # Retain actual eligible holdings first, then admit only top-five new candidates.
    cfg = dataset.config
    ranked = feat[feat.trend & feat.momentum.gt(0)].copy()
    ranked["code"] = ranked.index
    ranked = ranked.sort_values(["momentum", "code"], ascending=[False, True])
    ranks = {c: i + 1 for i, c in enumerate(ranked.index)}
    info = selected.set_index("etf_code")
    held = {c for c, q in (holdings or {}).items() if q > 1e-12}
    retained = [c for c in ranks if c in held and ranks[c] <= 8]
    if not cfg["rotation_buffer"]:
        retained = []
    candidates = retained + [c for c in ranks if ranks[c] <= 5 and c not in retained]
    chosen, decisions, themes = [], [], {}
    for code in candidates:
        reason = None
        category = info.at[code, "theme_group"] if "theme_group" in info else None
        category = category if pd.notna(category) and category else None
        comparisons = []
        if len(chosen) >= 5:
            reason = "slots_full"
        if cfg["rotation_constraints"] and reason is None:
            if category and themes.get(category, 0) >= 2:
                reason = "theme_limit"
            for other in chosen:
                if reason:
                    break
                common = dataset.returns.loc[:day, [code, other]].tail(126).dropna()
                if len(common) < 100:
                    reason = "insufficient_pair_history"
                    break
                correlation = common.corr().iloc[0, 1]
                comparisons.append({"other": other, "correlation": correlation,
                                    "observations": len(common)})
                if pd.isna(correlation):
                    reason = "invalid_pair_correlation"
                elif correlation >= .90:
                    reason = "correlated_exposure"
        if reason is None:
            chosen.append(code)
            if category:
                themes[category] = themes.get(category, 0) + 1
        decisions.append({"code": code, "rank": ranks[code], "selected": reason is None,
                          "reason": reason or ("retained" if code in retained else "entry"),
                          "comparisons": comparisons})
    exits = [{"code": c, "reason": "not_retained", "rank": ranks.get(c)}
             for c in sorted(held - set(chosen))]
    return {c: .2 for c in chosen}, {
        "ranking": [{"code": c, "rank": ranks[c], "score": ranked.at[c, "momentum"]}
                    for c in ranks], "selection": decisions, "exits": exits,
        "actual_holdings_input": sorted(held), "empty_slots": 5 - len(chosen)}
