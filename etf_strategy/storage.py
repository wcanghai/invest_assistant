"""Additive source migration and isolated research persistence."""

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CLASSES, digest


def now():
    # Return an explicit UTC collection timestamp.
    return datetime.now(timezone.utc).isoformat()


def connect(path, readonly=False):
    # Never create source databases from research read paths.
    target = Path(path).resolve()
    if readonly:
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(path):
    # Add ETF evidence tables and retain existing price rows unchanged.
    with connect(path) as conn:
        conn.executescript(Path(__file__).with_name("schema.sql").read_text())
        columns = {row[1] for row in conn.execute("PRAGMA table_info(etf_daily)")}
        if columns and "forward_factor" not in columns:
            conn.execute("ALTER TABLE etf_daily ADD COLUMN forward_factor REAL")
        additions = {
            "etf_classification": ("exposure_type", "theme_group", "published_at", "collected_at"),
            "etf_action": ("share_change_date", "resume_date", "published_at", "collected_at"),
        }
        for table, names in additions.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name in names:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")


def clean(value):
    # Convert pandas/numpy values to strict portable JSON without NaN.
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if value is pd.NA:
        return None
    return value


def save_run(path, command, config, payload):
    # Persist complete evidence with a deterministic content-addressed run identifier.
    result = clean(payload)
    run_id = digest({"command": command, "config": config, "result": result})
    with connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS research_run (run_id TEXT PRIMARY KEY, "
                     "command TEXT, config_json TEXT, result_json TEXT, created_at TEXT)")
        conn.execute("INSERT OR IGNORE INTO research_run VALUES(?,?,?,?,?)",
                     (run_id, command, json.dumps(config),
                      json.dumps(result, ensure_ascii=False, allow_nan=False), now()))
    return run_id, result


def import_records(path, table, records):
    # Validate typed evidence imports atomically rather than trusting arbitrary SQL fields.
    allowed = {"etf_classification", "etf_adjustment", "etf_action", "etf_validation",
               "etf_calendar", "etf_identity", "etf_rule", "etf_universe_validation"}
    if table not in allowed:
        raise ValueError("Unsupported evidence table")
    migrate(path)
    with connect(path) as conn:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        normalized = []
        for record in records:
            record = dict(record)
            optional = {"exposure_type", "theme_group", "published_at", "collected_at",
                        "share_change_date", "resume_date"}
            for key in set(columns) & optional:
                record.setdefault(key, None)
            normalized.append(record)
            if set(record) != set(columns):
                raise ValueError(f"Expected fields: {columns}")
            if not record.get("source"):
                raise ValueError("Evidence source is required")
            for key in ("verified", "historical_verified", "actions_complete",
                        "identity_complete", "trading_rules_verified", "is_t0",
                        "terminated_products_complete", "historical_mappings_complete"):
                if key in record and record[key] not in (0, 1):
                    raise ValueError(f"Invalid verification flag: {key}")
            for key in ("factor", "cash_per_share", "share_multiplier", "price_tick"):
                if key in record and not math.isfinite(record[key]):
                    raise ValueError(f"Nonfinite evidence number: {key}")
            for key, value in record.items():
                if key.endswith("date") or key in ("valid_from", "valid_to", "start_date",
                                                   "end_date", "known_at"):
                    if value is not None:
                        datetime.fromisoformat(value)
            if table == "etf_classification":
                if record["asset_class"] not in CLASSES:
                    raise ValueError("Invalid asset class")
                if record["pool"] not in ("base", "factor", "all_multi", "all_equity"):
                    raise ValueError("Invalid pool")
                if record["factor_style"] not in (None, "value", "quality", "low_vol"):
                    raise ValueError("Mixed or unsupported style")
            if table == "etf_action":
                if not record["record_date"] <= record["ex_date"] <= record["pay_date"]:
                    raise ValueError("Invalid action date order")
                if record["cash_per_share"] < 0:
                    raise ValueError("Negative distribution")
                if record.get("share_change_date") and record["cash_per_share"]:
                    if record["share_change_date"] != record["ex_date"]:
                        raise ValueError("Separate cash and split events when dates differ")
                if record.get("resume_date") and record.get("share_change_date"):
                    if record["resume_date"] < record["share_change_date"]:
                        raise ValueError("Resume cannot precede share change")
            if table == "etf_validation" and record["factor_direction"] != "multiply":
                raise ValueError("Only independently verified multiplicative factors supported")
            start = record.get("valid_from") or record.get("start_date")
            end = record.get("valid_to") or record.get("end_date")
            if start and end and start > end:
                raise ValueError("Reversed evidence interval")
        marks = ",".join("?" for _ in columns)
        conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES({marks})",
                         [tuple(r[k] for k in columns) for r in normalized])
        if table == "etf_identity":
            for record in records:
                code = record["etf_code"]
                if not code.endswith((".SH", ".SZ")):
                    raise ValueError("Only Shanghai/Shenzhen ETFs supported")
                conn.execute(
                    "INSERT OR IGNORE INTO etf_master(etf_code,exchange,list_date,"
                    "first_seen_date,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (code, code[-2:], record["list_date"], now()[:10], now(), now()))
    return len(records)
