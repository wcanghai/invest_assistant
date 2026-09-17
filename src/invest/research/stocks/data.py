"""多因子策略的股票池、行情、市值和时点财务加工。"""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from invest.storage import factor as db


FLOW_FIELDS = (
    "revenue",
    "net_profit",
    "parent_net_profit",
    "deduct_net_profit",
    "operating_cash_flow",
)


def _date_before(value: str, days: int) -> str:
    # 返回指定日期之前若干自然日的 ISO 日期。
    return (date.fromisoformat(value) - timedelta(days=days)).isoformat()


def _latest_financial_versions(frame: pd.DataFrame) -> pd.DataFrame:
    # 对每个报告期选择当前因子日已公告的最后版本。
    if frame.empty:
        return frame.copy()
    ordered = frame.sort_values(["stock_code", "report_date", "announce_date"])
    return ordered.groupby(["stock_code", "report_date"], as_index=False).tail(1)


def _row_for_date(frame: pd.DataFrame, report_date: str) -> pd.Series | None:
    # 返回指定报告期记录，不存在时返回空。
    matched = frame.loc[frame["report_date"] == report_date]
    return None if matched.empty else matched.iloc[-1]


def _ttm_value(frame: pd.DataFrame, latest: pd.Series, field: str) -> float:
    # 根据最新累计报告、上年年报和上年同期报告计算 TTM。
    value = pd.to_numeric(pd.Series([latest.get(field)]), errors="coerce").iloc[0]
    if pd.isna(value):
        return np.nan
    report = date.fromisoformat(str(latest["report_date"]))
    if report.month == 12:
        return float(value)
    previous_year = report.year - 1
    annual = _row_for_date(frame, f"{previous_year}-12-31")
    prior_same = _row_for_date(
        frame,
        f"{previous_year}-{report.month:02d}-{report.day:02d}",
    )
    if annual is None or prior_same is None:
        return np.nan
    annual_value = pd.to_numeric(pd.Series([annual.get(field)]), errors="coerce").iloc[0]
    prior_value = pd.to_numeric(pd.Series([prior_same.get(field)]), errors="coerce").iloc[0]
    if pd.isna(annual_value) or pd.isna(prior_value):
        return np.nan
    return float(annual_value + value - prior_value)


def _safe_growth(current: float, previous: float) -> float:
    # 计算正常正基期同比，异常基期返回缺失。
    if not np.isfinite(current) or not np.isfinite(previous) or previous <= 0:
        return np.nan
    return current / previous - 1.0


def _number(value: Any) -> float:
    # 将任意财务值安全转换为浮点数。
    converted = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return float(converted) if pd.notna(converted) else np.nan


def _financial_for_stock(frame: pd.DataFrame) -> dict[str, Any]:
    # 将一只股票已公告财务记录转换为最新 TTM 和稳定性特征。
    ordered = frame.sort_values("report_date")
    latest = ordered.iloc[-1]
    result: dict[str, Any] = {
        "stock_code": latest["stock_code"],
        "report_date": latest["report_date"],
        "announce_date": latest["announce_date"],
    }
    for field in FLOW_FIELDS:
        result[f"{field}_ttm"] = _ttm_value(ordered, latest, field)
    for field in (
        "total_assets",
        "total_liabilities",
        "total_equity",
        "roe_pct",
        "gross_margin_pct",
        "debt_ratio_pct",
    ):
        result[field] = pd.to_numeric(
            pd.Series([latest.get(field)]), errors="coerce"
        ).iloc[0]
    if len(ordered) >= 2:
        previous = ordered.iloc[-2]
        result["assets_average"] = np.nanmean(
            [result["total_assets"], previous.get("total_assets")]
        )
        result["equity_average"] = np.nanmean(
            [result["total_equity"], previous.get("total_equity")]
        )
    else:
        result["assets_average"] = result["total_assets"]
        result["equity_average"] = result["total_equity"]
    report = date.fromisoformat(str(latest["report_date"]))
    prior_report = _row_for_date(
        ordered,
        f"{report.year - 1}-{report.month:02d}-{report.day:02d}",
    )
    result["revenue_yoy"] = np.nan
    result["parent_profit_yoy"] = np.nan
    result["deduct_profit_yoy"] = np.nan
    if prior_report is not None:
        for output, field in (
            ("revenue_yoy", "revenue"),
            ("parent_profit_yoy", "parent_net_profit"),
            ("deduct_profit_yoy", "deduct_net_profit"),
        ):
            result[output] = _safe_growth(
                _number(latest.get(field)),
                _number(prior_report.get(field)),
            )
    recent = ordered.tail(8)
    result["profit_stability"] = -pd.to_numeric(
        recent["roe_pct"], errors="coerce"
    ).std()
    result["growth_stability"] = -pd.to_numeric(
        recent["revenue_growth_pct"], errors="coerce"
    ).std()
    same_period = ordered.loc[
        pd.to_datetime(ordered["report_date"]).dt.strftime("%m-%d")
        == report.strftime("%m-%d")
    ]
    three_year = _row_for_date(
        same_period,
        f"{report.year - 3}-{report.month:02d}-{report.day:02d}",
    )
    result["revenue_cagr_3y"] = np.nan
    if three_year is not None:
        old_revenue = _number(three_year.get("revenue"))
        new_revenue = _number(latest.get("revenue"))
        if old_revenue > 0 and new_revenue > 0:
            result["revenue_cagr_3y"] = (new_revenue / old_revenue) ** (1 / 3) - 1
    return result


def build_financial_features(frame: pd.DataFrame) -> pd.DataFrame:
    # 为财务记录按股票生成时点正确的最新特征。
    versions = _latest_financial_versions(frame)
    if versions.empty:
        return pd.DataFrame(index=pd.Index([], name="stock_code"))
    rows = [_financial_for_stock(group) for _, group in versions.groupby("stock_code")]
    return pd.DataFrame(rows).set_index("stock_code")


def _prepare_bars(frame: pd.DataFrame) -> pd.DataFrame:
    # 清洗行情并计算复权价格、收益和成交额人民币值。
    if frame.empty:
        return frame.copy()
    bars = frame.copy()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    for field in ("close", "volume", "amount_10k", "forward_factor"):
        bars[field] = pd.to_numeric(bars[field], errors="coerce")
    bars["adj_close"] = bars["close"] * bars["forward_factor"]
    bars["return"] = bars.groupby("stock_code")["adj_close"].pct_change(
        fill_method=None
    )
    bars["amount"] = bars["amount_10k"] * 10_000.0
    return bars.sort_values(["stock_code", "trade_date"])


def _latest_capital(frame: pd.DataFrame) -> pd.DataFrame:
    # 选择每只股票因子日前最近股本。
    if frame.empty:
        return pd.DataFrame(columns=["stock_code", "total_shares", "float_shares"])
    ordered = frame.sort_values(["stock_code", "trade_date"])
    return ordered.groupby("stock_code", as_index=False).tail(1)


def _capital_from_snapshot(snapshot: pd.DataFrame, factor_date: str) -> pd.DataFrame:
    # 使用历史证券快照中的万股口径构造因子日股本。
    columns = ["stock_code", "total_shares_10k", "float_shares_10k"]
    capital = snapshot[columns].copy()
    capital["trade_date"] = factor_date
    capital["total_shares"] = pd.to_numeric(
        capital["total_shares_10k"], errors="coerce"
    ) * 10_000.0
    capital["float_shares"] = pd.to_numeric(
        capital["float_shares_10k"], errors="coerce"
    ) * 10_000.0
    return capital[["stock_code", "trade_date", "total_shares", "float_shares"]]


def _build_universe(
    snapshot: pd.DataFrame,
    bars: pd.DataFrame,
    capital: pd.DataFrame,
    config: dict[str, Any],
) -> pd.DataFrame:
    # 应用证券状态、历史长度和流动性过滤并记录排除原因。
    if snapshot.empty:
        return pd.DataFrame(index=pd.Index([], name="stock_code"))
    universe = snapshot.set_index("stock_code").copy()
    reasons: dict[str, list[str]] = {code: [] for code in universe.index}
    flag_rules = {
        "NOT_ACTIVE": universe["is_active"].fillna(0) != 1,
        "NOT_A_SHARE": universe["is_all_a"].fillna(0) != 1,
        "NOT_TRADABLE": universe["is_tradable"].fillna(0) != 1,
        "ST": universe["is_st"].fillna(0) == 1,
        "DELISTING": universe["is_delisting_board"].fillna(0) == 1,
        "SUSPENDED": universe["is_suspended"].fillna(0) == 1,
        "MISSING_INDUSTRY": universe["industry_code"].isna(),
    }
    for reason, mask in flag_rules.items():
        for code in universe.index[mask]:
            reasons[code].append(reason)
    grouped = bars.groupby("stock_code")
    counts = grouped.size()
    recent = grouped.tail(20)
    recent_stats = recent.groupby("stock_code").agg(
        trading_days_20=("is_trading", "sum"),
        adv_20=("amount", "mean"),
    )
    universe = universe.join(counts.rename("bar_count")).join(recent_stats)
    latest_capital = _latest_capital(capital).set_index("stock_code")
    universe = universe.join(latest_capital[["total_shares", "float_shares"]])
    latest_bars = grouped.tail(1).set_index("stock_code")
    universe = universe.join(latest_bars[["close"]])
    universe["total_cap"] = universe["close"] * universe["total_shares"]
    universe["float_cap"] = universe["close"] * universe["float_shares"]
    settings = config["universe"]
    for code, row in universe.iterrows():
        if pd.isna(row["bar_count"]) or row["bar_count"] < settings["min_listing_days"]:
            reasons[code].append("INSUFFICIENT_BARS")
        if row.get("trading_days_20", 0) < settings["min_trading_days_20"]:
            reasons[code].append("LOW_TRADING_DAYS")
        if pd.isna(row.get("adv_20")) or row["adv_20"] < settings["min_adv_10k"] * 10_000:
            reasons[code].append("LOW_LIQUIDITY")
        if pd.isna(row.get("close")) or row["close"] <= 0:
            reasons[code].append("INVALID_PRICE")
        if pd.isna(row.get("float_cap")) or row["float_cap"] <= 0:
            reasons[code].append("INVALID_CAPITAL")
    initially_valid = [code for code, value in reasons.items() if not value]
    if initially_valid:
        threshold = universe.loc[initially_valid, "adv_20"].quantile(
            settings["exclude_bottom_liquidity_pct"]
        )
        for code in initially_valid:
            if universe.at[code, "adv_20"] < threshold:
                reasons[code].append("LOW_LIQUIDITY_PERCENTILE")
    universe["exclusion_reason"] = [",".join(reasons[code]) for code in universe.index]
    universe["universe_flag"] = (universe["exclusion_reason"] == "").astype(int)
    return universe


def load_factor_data(
    connection: Any,
    factor_date: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    # 读取并组装指定因子日全部因子所需数据。
    dates = db.load_trade_dates(
        connection,
        factor_date,
        int(config["universe"]["lookback_days"]),
    )
    if not dates or dates[-1] != factor_date:
        raise ValueError(f"{factor_date} 不是数据库中的交易日")
    start_date = dates[0]
    snapshot = db.load_snapshot(connection, factor_date)
    codes = snapshot["stock_code"].astype(str).tolist() if not snapshot.empty else []
    bars = _prepare_bars(db.load_bars(connection, codes, start_date, factor_date))
    capital = _capital_from_snapshot(snapshot, factor_date)
    trade = db.load_trade_metrics(connection, codes, start_date, factor_date)
    actions = db.load_actions(
        connection,
        codes,
        _date_before(factor_date, 400),
        factor_date,
    )
    financial_raw = db.load_financials_asof(connection, codes, factor_date)
    financial = build_financial_features(financial_raw)
    universe = _build_universe(snapshot, bars, capital, config)
    return {
        "factor_date": factor_date,
        "next_trade_date": db.next_trade_date(connection, factor_date),
        "universe": universe,
        "bars": bars,
        "capital": capital,
        "financial": financial,
        "trade": trade,
        "actions": actions,
    }
