"""Explicit data supplementation; vendor events stay raw until verified."""

import json

from invest.storage.etf import connect
from invest.storage.etf import import_records
from invest.storage.etf import migrate
from invest.storage.etf import now


def collect(path, codes, start, end, raw_output, client=None):
    # Preserve factors and provenance while refusing to invent units or missing dividend dates.
    from invest.providers.security_pool import init_tdx
    from invest.providers.stock import fetch_daily_bars
    from invest.market.stock.processor import flatten_daily_bars
    from invest.market.security_pool.processor import parse_date

    client = client or init_tdx()
    migrate(path)
    evidence, adjustments, calendar, errors, recovered = {}, [], [], [], []
    timestamp = now()
    for code in codes:
        try:
            rows = flatten_daily_bars(fetch_daily_bars(client, [code], start, end))
            for row in rows:
                if row.get("close") is not None and row["close"] > 0:
                    recovered.append({**row, "etf_code": code})
                factor = row.get("forward_factor")
                if factor is not None and factor > 0:
                    adjustments.append({"etf_code": code, "trade_date": row["trade_date"],
                                        "factor": factor, "source": "tdx.ForwardFactor",
                                        "fetched_at": timestamp})
            events = client.get_divid_factors(code, start.replace("-", ""),
                                              end.replace("-", ""))
            evidence[code] = (events.to_json(orient="split")
                              if hasattr(events, "to_json") else events)
        except Exception as exc:
            errors.append({"code": code, "error": str(exc)})
    try:
        dates = client.get_trading_dates("SH", start.replace("-", ""), end.replace("-", ""))
        for value in dates:
            parsed = parse_date(value)
            if parsed:
                calendar.append({"trade_date": parsed, "source": "tdx.get_trading_dates.SH",
                                 "verified": 1})
    except Exception as exc:
        errors.append({"domain": "calendar", "error": str(exc)})
    import_records(path, "etf_adjustment", adjustments)
    import_records(path, "etf_calendar", calendar)
    with connect(path) as conn:
        # Supplement null cells only; never revise pre-existing OHLC history silently.
        columns = ("etf_code", "trade_date", "open", "high", "low", "close", "pre_close",
                   "pct_change", "volume", "amount_10k", "is_trading", "updated_at")
        updates = ",".join(f"{c}=COALESCE(etf_daily.{c},excluded.{c})" for c in columns[2:])
        placeholders = ",".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO etf_daily({','.join(columns)}) VALUES({placeholders}) "
            f"ON CONFLICT(etf_code,trade_date) DO UPDATE SET {updates}",
            [tuple(r.get(k) for k in columns) for r in recovered])
        conn.executemany("UPDATE etf_daily SET forward_factor=? "
                         "WHERE etf_code=? AND trade_date=?",
                         [(r["factor"], r["etf_code"], r["trade_date"]) for r in adjustments])
        conn.executemany("INSERT OR REPLACE INTO etf_daily_provenance VALUES(?,?,?,?,?,?)",
                         [(r["etf_code"], r["trade_date"], "adjustment", None, timestamp,
                           "tdx.ForwardFactor") for r in adjustments])
        conn.executemany("INSERT OR REPLACE INTO etf_daily_provenance VALUES(?,?,?,?,?,?)",
                         [(r["etf_code"], r["trade_date"], "bar", None, timestamp,
                           "tdx.missing_cell_backfill") for r in recovered])
    report = {"raw_events": evidence, "errors": errors, "factor_rows": len(adjustments),
              "calendar_rows": len(calendar), "backfill_candidates": len(recovered),
              "fetched_at": timestamp,
              "validation": "not_certified; verify direction, units and complete event dates"}
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    raw_output.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2),
                          encoding="utf-8")
    return {k: v for k, v in report.items() if k != "raw_events"}
