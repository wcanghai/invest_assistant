"""Point-in-time views of source data and auditable eligibility decisions."""

import json

import numpy as np
import pandas as pd

from .config import digest
from .events import event_checks
from .storage import clean, connect


def read_optional(conn, table):
    # Read evidence when present without creating source tables.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
    return pd.read_sql_query(f"SELECT * FROM {table}", conn) if exists else pd.DataFrame()


def audit(path):
    # Measure actual field coverage and enumerate explicit implementation blockers.
    with connect(path, True) as conn:
        summary = dict(conn.execute(
            "SELECT count(*) rows,count(distinct etf_code) codes,min(trade_date) start,"
            "max(trade_date) end,sum(close>0) valid_close,"
            "max(CASE WHEN unit_nav>0 THEN trade_date END) nav_end,"
            "max(CASE WHEN size_10k>0 THEN trade_date END) size_end FROM etf_daily"
        ).fetchone())
        master = dict(conn.execute(
            "SELECT count(*) codes,sum(list_date IS NULL) missing_listing,"
            "sum(underlying_code IS NULL) missing_underlying FROM etf_master"
        ).fetchone())
        lengths = pd.read_sql_query(
            "SELECT etf_code,count(*) n FROM etf_daily WHERE close>0 GROUP BY etf_code", conn
        )
        evidence = {name: len(read_optional(conn, name)) for name in (
            "etf_adjustment", "etf_action", "etf_validation", "etf_classification",
            "etf_calendar", "etf_identity", "etf_rule", "etf_universe_validation")}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(etf_daily)")}
        factor_count = conn.execute(
            "SELECT count(*) FROM etf_daily WHERE forward_factor>0"
        ).fetchone()[0] if "forward_factor" in columns else 0
    return {
        "daily": summary, "master": master, "history_253": int((lengths.n >= 253).sum()),
        "evidence_rows": evidence, "stored_forward_factors": factor_count,
        "quality_level": "exploratory", "quality_scope": "source_inventory_not_run_certification",
        "blockers": ["Run-level factor/action verification required",
                     "Historical universe completeness must be evidenced",
                     "NAV/size publication times unavailable for legacy rows",
                     "Daily OHLC cannot establish actual order fills"],
        "feasibility": {name: "research_implementation_available_data_gate_required" for name in
                        ("dual_momentum", "trend", "risk_parity", "strategic")}
                       | {"factor_rotation": "conditional_three_verified_pure_styles"},
    }


class Dataset:
    """Load immutable local snapshots; all feature reads truncate at signal date."""

    def __init__(self, path, config, end=None):
        # Limit raw history to the run cutoff and fingerprint exact consumed inputs.
        self.config = config
        with connect(path, True) as conn:
            self.master = pd.read_sql_query("SELECT * FROM etf_master", conn)
            self.classes = read_optional(conn, "etf_classification")
            self.actions = read_optional(conn, "etf_action")
            self.validation = read_optional(conn, "etf_validation")
            self.universe_validation = read_optional(conn, "etf_universe_validation")
            self.identity = read_optional(conn, "etf_identity")
            self.rules = read_optional(conn, "etf_rule")
            self.calendar = read_optional(conn, "etf_calendar")
            factors = read_optional(conn, "etf_adjustment")
            cutoff = end or conn.execute(
                "SELECT max(trade_date) FROM etf_daily WHERE close>0"
            ).fetchone()[0]
            self.end = cutoff
            dates = pd.read_sql_query(
                "SELECT DISTINCT trade_date FROM etf_daily WHERE close>0 "
                "AND trade_date<=? ORDER BY trade_date", conn, params=(cutoff,)
            ).trade_date.tolist()
            verified_dates = self.calendar
            if not verified_dates.empty:
                verified_dates = verified_dates[verified_dates.verified.eq(1)]
            verified_set = (set(verified_dates.trade_date)
                            if not verified_dates.empty else set())
            self.calendar_verified = bool(dates) and set(dates).issubset(verified_set)
            # A partial calendar must not erase older history or silently shorten lookbacks.
            self.all_dates = sorted(set(dates) | verified_set)
            self.dates = pd.Index([d for d in self.all_dates if d <= cutoff])
            # Full master remains auditable; only classified candidates need price matrices.
            codes = sorted(self.master.etf_code.unique())
            frames = []
            for offset in range(0, len(codes), 400):
                batch = codes[offset:offset + 400]
                marks = ",".join("?" for _ in batch)
                frames.append(pd.read_sql_query(
                    "SELECT etf_code,trade_date,open,high,low,close,volume,amount_10k,"
                    "is_trading,forward_factor,size_10k FROM etf_daily "
                    f"WHERE etf_code IN ({marks}) "
                    "AND trade_date<=? ORDER BY etf_code,trade_date", conn,
                    params=(*batch, cutoff)))
        self.bars = {}
        for frame in frames:
            for code, group in frame.groupby("etf_code", sort=True):
                bar = group.drop(columns="etf_code").set_index("trade_date").reindex(self.dates)
                if "forward_factor" not in bar:
                    bar["forward_factor"] = np.nan
                if not factors.empty:
                    subset = factors[factors.etf_code.eq(code)].set_index("trade_date")
                    bar["forward_factor"] = subset.factor.reindex(self.dates).combine_first(
                        bar.forward_factor)
                valid = (bar.close.gt(0) & bar.is_trading.eq(1) & bar.volume.gt(0))
                bar["valid"] = valid
                if config["price_mode"] != "exploratory":
                    bar["signal_price"] = (bar.close * bar.forward_factor).where(
                        valid & bar.forward_factor.gt(0))
                else:
                    bar["signal_price"] = bar.close.where(valid)
                self.bars[code] = bar
        self.prices = pd.DataFrame({c: b.signal_price for c, b in self.bars.items()},
                                   index=self.dates)
        self.returns = self.prices.pct_change(fill_method=None)
        self.action_issues = {}
        self._universe_cache = {}
        self._records_cache = {}
        self._bar_stats = {}
        for code, bar in self.bars.items():
            events = self.actions[self.actions.etf_code.eq(code)] if not self.actions.empty \
                else self.actions
            self.action_issues[code] = event_checks(bar, events, cutoff)[1]
        self.metadata = {
            "end": cutoff, "calendar_verified": self.calendar_verified,
            "universe_mode": config["universe_mode"], "price_mode": config["price_mode"],
            "classification_hash": digest(clean(self.classes.to_dict("records"))),
            "data_hash": digest({
                "bars": [pd.util.hash_pandas_object(f).sum().item() for f in frames],
                "evidence": [digest(clean(f.to_dict("records"))) for f in
                             (self.master, factors, self.actions, self.validation,
                              self.identity, self.rules, self.calendar,
                              self.universe_validation)]}),
            "limitations": ["Survivorship bias" if config["universe_mode"] == "survivors"
                            else "Historical identity evidence required",
                            "Unadjusted exploration" if config["price_mode"] == "exploratory"
                            else "Validated factors required",
                            "OHLC execution model; no order book"],
        }

    def classification(self, day):
        # Select effective records without silently using future historical classifications.
        frame = self.classes
        if frame.empty:
            return frame
        mask = frame.valid_from.le(day) & (frame.valid_to.isna() | frame.valid_to.ge(day))
        mask &= frame.verified.eq(1)
        if self.config["universe_mode"] == "point_in_time":
            published = frame.get("published_at", frame.known_at).fillna(frame.known_at)
            mask &= published.str[:10].le(day) & frame.historical_verified.eq(1)
        return frame[mask].sort_values("known_at").drop_duplicates("etf_code", keep="last")

    def validation_ok(self, code, start, day):
        # Certification must cover the entire consumed history and company actions.
        frame = self.validation
        if frame.empty:
            return False
        rows = frame[frame.etf_code.eq(code) & frame.start_date.le(start)
                     & frame.end_date.ge(day) & frame.actions_complete.eq(1)
                     & frame.factor_direction.eq("multiply")]
        bar = self.bars.get(code)
        if bar is None or rows.empty:
            return False
        observed = bar.loc[start:day]
        if (observed.valid & ~observed.forward_factor.gt(0)).any():
            return False
        events = self.actions[self.actions.etf_code.eq(code)] if not self.actions.empty \
            else self.actions
        return not event_checks(bar.loc[start:day], events, day)[1]


    def history_metrics(self, code, day):
        # Compute rolling gates once per asset and retain only rebalance-date snapshots.
        cfg = self.config
        windows = (cfg["liquidity_window"], cfg["coverage_window"], cfg["dedup_window"])
        key = (code, windows)
        cached = self._bar_stats.get(key, {})
        if day in cached:
            return cached[day]
        bar = self.bars[code]
        liquid, coverage, dedup = windows
        amount = bar.amount_10k.where(bar.is_trading.ne(0), 0)
        sizes = bar.size_10k.where(bar.size_10k.gt(0))
        stats = pd.DataFrame({
            "valid_history": bar.valid.cumsum(),
            "coverage": bar.valid.rolling(coverage, min_periods=1).sum() / coverage,
            "recent_trading_days": bar.valid.rolling(liquid, min_periods=1).sum(),
            "adv20_10k": amount.rolling(liquid, min_periods=1).mean(),
            "adv60_10k": amount.rolling(dedup, min_periods=1).mean(),
            "unknown_amount": amount.isna().rolling(liquid, min_periods=1).sum().gt(0),
            "size_10k": sizes.ffill(),
            "size_date": pd.Series(bar.index, index=bar.index).where(sizes.notna()).ffill(),
        })
        observed_dates = bar.index[bar.valid]
        stats["first_valid"] = observed_dates[0] if len(observed_dates) else None
        dates = [d for d, nxt in zip(self.all_dates, self.all_dates[1:]) if d[:7] != nxt[:7]]
        dates = sorted(set(dates + [day, self.end]) & set(stats.index))
        cached.update(stats.loc[dates].to_dict("index"))
        self._bar_stats[key] = cached
        return cached[day]


    def universe_key(self, day, pool):
        # Share immutable screening snapshots across cost and portfolio experiments.
        cfg = self.config
        selection_keys = ("price_mode", "universe_mode", "min_history", "coverage_window",
                          "min_coverage", "liquidity_window", "min_trading_days", "min_adv_10k",
                          "dedup_window", "allowed_codes")
        return (day, pool, digest({k: cfg[k] for k in selection_keys}))


    def universe_records(self, day, pool, frame):
        # Avoid repeatedly serializing the same all-market screening rows per strategy.
        key = self.universe_key(day, pool)
        if key not in self._records_cache:
            self._records_cache[key] = clean(frame.to_dict("records"))
        return self._records_cache[key]


    def universe(self, day, pool="base"):
        # Retain one row per master security with all failed gates and historical diagnostics.
        cfg = self.config
        cache_key = self.universe_key(day, pool)
        if cache_key in self._universe_cache:
            return self._universe_cache[cache_key].copy(deep=True)
        classes = self.classification(day)
        mapping = classes.set_index("etf_code").to_dict("index") if not classes.empty else {}
        records = []
        for item in self.master.to_dict("records"):
            code = item["etf_code"]
            info = mapping.get(code, {})
            reasons = []
            row = {"etf_code": code, "etf_name": item["etf_name"], "date": day,
                   **info, "valid_history": 0, "adv20_10k": None, "adv60_10k": None}
            broad = pool in ("all_multi", "all_equity")
            if broad and not self.calendar_verified:
                reasons.append("calendar_unverified")
            pool_ok = bool(info) and (broad or info.get("pool") == pool)
            if pool == "all_equity" and info.get("asset_class") != "equity":
                pool_ok = False
            if not pool_ok:
                reasons.append("classification_unverified_or_wrong_pool")
            if cfg["allowed_codes"] is not None and code not in cfg["allowed_codes"]:
                reasons.append("outside_frozen_reference_pool")
            if broad and info.get("asset_class") == "equity":
                if info.get("exposure_type") not in ("broad", "sector", "theme", "factor"):
                    reasons.append("exposure_type_unverified")
                if info.get("exposure_type") in ("sector", "theme"):
                    if not info.get("theme_group"):
                        reasons.append("theme_group_unverified")
            structures = {"bond": ("government", "policy_bank"), "gold": ("physical",),
                          "commodity": ("futures",)}
            if broad and info.get("asset_class") in structures:
                if info.get("exposure_type") not in structures[info["asset_class"]]:
                    reasons.append("asset_structure_unverified")
            listing = item["list_date"]
            if cfg["universe_mode"] == "point_in_time":
                identities = self.identity
                match = identities[identities.etf_code.eq(code)] if not identities.empty else []
                if len(match) != 1:
                    reasons.append("historical_identity_missing")
                else:
                    identity = match.iloc[0]
                    listing = identity.list_date
                    if (not identity.verified or identity.known_at[:10] > day
                            or (pd.notna(identity.end_date) and identity.end_date < day)):
                        reasons.append("historical_identity_invalid")
            if pd.isna(listing) or not listing or listing > day:
                reasons.append("not_listed_or_unknown_listing")
            bar = self.bars.get(code)
            if bar is None or day not in bar.index:
                reasons.append("no_history")
            else:
                row.update(self.history_metrics(code, day))
                if row["valid_history"] < cfg["min_history"]:
                    reasons.append("insufficient_history")
                if row["coverage"] < cfg["min_coverage"]:
                    reasons.append("poor_coverage")
                if not bar.at[day, "valid"]:
                    reasons.append("invalid_signal_day")
                if row["recent_trading_days"] < cfg["min_trading_days"]:
                    reasons.append("insufficient_recent_trading")
                if row["unknown_amount"]:
                    reasons.append("unknown_liquidity")
                if not row["adv20_10k"] >= cfg["min_adv_10k"]:
                    reasons.append("low_liquidity")
                if cfg["price_mode"] != "exploratory" or broad:
                    first = row["first_valid"] or day
                    if not self.validation_ok(code, first, day):
                        reasons.append("adjustment_or_action_unverified")
            row["reasons"] = reasons
            row["eligible"] = not reasons
            records.append(row)
        result = pd.DataFrame(records)
        if not result.empty and result.eligible.any():
            candidates = result[result.eligible].sort_values(
                ["adv60_10k", "etf_code"], ascending=[False, True])
            duplicate = candidates.duplicated("exposure_group")
            for index in candidates.index[duplicate]:
                result.at[index, "eligible"] = False
                result.at[index, "reasons"] = ["duplicate_exposure"]
        self._universe_cache[cache_key] = result
        return result.copy(deep=True)

    def quality(self, day, codes):
        # Never promote a run without verified calendar, prices and point-in-time identities.
        if (not codes or self.config["price_mode"] != "validated"
                or self.config["universe_mode"] != "point_in_time"
                or not self.calendar_verified):
            return "exploratory"
        evidence = self.universe_validation
        if evidence.empty:
            return "exploratory"
        complete = evidence[evidence.start_date.le(self.dates[0]) & evidence.end_date.ge(day)
                            & evidence.terminated_products_complete.eq(1)
                            & evidence.historical_mappings_complete.eq(1)]
        if complete.empty:
            return "exploratory"
        for code in codes:
            history = self.bars[code].loc[:day]
            observed = history[history.valid]
            if observed.empty or not self.validation_ok(code, observed.index[0], day):
                return "exploratory"
            identity = self.identity
            if identity.empty:
                return "exploratory"
            row = identity[identity.etf_code.eq(code) & identity.verified.eq(1)
                           & identity.known_at.str[:10].le(day)]
            if len(row) != 1:
                return "exploratory"
        return "validated_research"
