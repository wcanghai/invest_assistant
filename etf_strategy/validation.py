"""Price evidence diagnostics sharing the ledger's suspension-aware event alignment."""

import pandas as pd

from .data import read_optional
from .events import event_checks
from .storage import connect


def validate_prices(path, codes):
    # Read factors from both additive storage and source bars, without certifying completeness.
    result = {}
    with connect(path, True) as conn:
        actions = read_optional(conn, "etf_action")
        adjustments = read_optional(conn, "etf_adjustment")
        for code in codes:
            frame = pd.read_sql_query(
                "SELECT trade_date,close,forward_factor,is_trading,volume FROM etf_daily "
                "WHERE etf_code=? ORDER BY trade_date", conn, params=(code,)
            ).set_index("trade_date")
            if frame.empty:
                result[code] = {"status": "missing_prices"}
                continue
            if not adjustments.empty:
                extra = adjustments[adjustments.etf_code.eq(code)].set_index("trade_date")
                frame["forward_factor"] = extra.factor.reindex(frame.index).combine_first(
                    frame.forward_factor)
            frame["valid"] = frame.close.gt(0) & frame.is_trading.eq(1) & frame.volume.gt(0)
            events = actions[actions.etf_code.eq(code)] if not actions.empty else actions
            checks, issues = event_checks(frame, events, frame.index[-1])
            result[code] = {"observations": int(frame.valid.sum()),
                            "factor_observations": int(frame.forward_factor.gt(0).sum()),
                            "event_checks": checks, "issues": issues,
                            "status": "requires_independent_completeness_review"}
    return result
