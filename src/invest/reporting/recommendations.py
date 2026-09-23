"""Generate current, in-memory stock research recommendations."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from invest.research.stocks.config import load_config
from invest.research.stocks.data import load_factor_data
from invest.research.stocks.scoring import calculate_scores


PROFILE_WEIGHTS = {
    "conservative": {
        "value": 0.23,
        "quality": 0.27,
        "growth": 0.10,
        "momentum": 0.10,
        "low_risk": 0.20,
        "flow_sentiment": 0.04,
        "liquidity": 0.06,
    },
    "balanced": {
        "value": 0.20,
        "quality": 0.20,
        "growth": 0.15,
        "momentum": 0.20,
        "low_risk": 0.10,
        "flow_sentiment": 0.10,
        "liquidity": 0.05,
    },
    "aggressive": {
        "value": 0.10,
        "quality": 0.13,
        "growth": 0.24,
        "momentum": 0.28,
        "low_risk": 0.05,
        "flow_sentiment": 0.15,
        "liquidity": 0.05,
    },
}

PROFILE_NAMES = {
    "conservative": "稳健型",
    "balanced": "均衡型",
    "aggressive": "进取型",
}

CATEGORY_NAMES = {
    "value": "估值",
    "quality": "质量",
    "growth": "成长",
    "momentum": "动量",
    "low_risk": "低风险",
    "flow_sentiment": "资金情绪",
    "liquidity": "流动性",
}

CORE_CATEGORIES = ("value", "quality", "growth", "momentum")


def connect_market(path: str | Path) -> sqlite3.Connection:
    # 在内存连接中只读挂载市场库，避免生成任何推荐历史。
    market_path = Path(path).resolve()
    if not market_path.is_file() or not market_path.stat().st_size:
        raise FileNotFoundError(f"市场数据库不存在或为空: {market_path}")
    connection = sqlite3.connect(":memory:", timeout=30, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "ATTACH DATABASE ? AS market_source",
        (market_path.as_uri() + "?mode=ro",),
    )
    tables = (
        "stock_master", "stock_current", "stock_daily_bar", "stock_capital_daily",
        "stock_financial_report", "stock_trade_daily", "stock_sector_snapshot",
        "stock_snapshot", "stock_corporate_action",
    )
    for table in tables:
        connection.execute(
            f'CREATE TEMP VIEW "{table}" AS SELECT * FROM market_source."{table}"'
        )
    return connection


def latest_complete_date(connection: sqlite3.Connection) -> str:
    # 选择股票快照与日线覆盖率均达到九成的最近交易日。
    row = connection.execute(
        """
        WITH dates AS (
          SELECT DISTINCT trade_date FROM stock_daily_bar
          ORDER BY trade_date DESC LIMIT 10
        )
        SELECT trade_date FROM dates
        WHERE (SELECT COUNT(*) FROM stock_daily_bar b
               WHERE b.trade_date=dates.trade_date) >= 0.9 *
              (SELECT COUNT(*) FROM stock_snapshot s
               WHERE s.snapshot_date=dates.trade_date
                 AND s.is_active=1 AND s.is_all_a=1)
          AND EXISTS (SELECT 1 FROM stock_snapshot s
                      WHERE s.snapshot_date=dates.trade_date)
        ORDER BY trade_date DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("没有找到行情与证券快照均完整的交易日")
    return str(row[0])


def _finite(value: Any, digits: int = 4) -> float | None:
    # 将数值转换为 JSON 可用的有限浮点数。
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def _period_return(group: pd.DataFrame, periods: int) -> float | None:
    # 计算指定交易日窗口的复权收益率百分比。
    prices = group.sort_values("trade_date")["adj_close"].dropna()
    if len(prices) <= periods or prices.iloc[-periods - 1] <= 0:
        return None
    return _finite(100 * (prices.iloc[-1] / prices.iloc[-periods - 1] - 1), 2)


def _profile_score(scores: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    # 按每只股票的有效类别重新归一化固定类型权重。
    columns = [f"{name}_score" for name in weights]
    frame = scores[columns]
    weight_values = pd.Series(
        {f"{name}_score": value for name, value in weights.items()}
    )
    numerator = frame.mul(weight_values, axis=1).sum(axis=1, min_count=1)
    denominator = frame.notna().mul(weight_values, axis=1).sum(axis=1)
    result = numerator / denominator.where(denominator > 0)
    core = [f"{name}_score" for name in CORE_CATEGORIES]
    result = result.where(frame[core].notna().sum(axis=1) >= 3)
    return result.where(scores["universe_flag"] == 1)


def _factor_summary(row: pd.Series) -> tuple[list[str], list[str]]:
    # 根据实际类别分选择优势与弱项，生成确定性说明。
    values = {
        name: row.get(f"{name}_score")
        for name in CATEGORY_NAMES
        if pd.notna(row.get(f"{name}_score"))
    }
    ordered = sorted(values, key=lambda name: (-float(values[name]), name))
    strengths = [f"{CATEGORY_NAMES[name]}因子较强" for name in ordered[:3]]
    weak = sorted(values, key=lambda name: (float(values[name]), name))[:2]
    risks = [f"{CATEGORY_NAMES[name]}因子相对偏弱" for name in weak]
    return strengths, risks


def _exclusion_counts(scores: pd.DataFrame) -> dict[str, int]:
    # 汇总股票池规则与核心因子缺失造成的排除数量。
    counts: Counter[str] = Counter()
    for value in scores["exclusion_reason"].fillna(""):
        for reason in str(value).split(","):
            if reason:
                counts[reason] += 1
    core = [f"{name}_score" for name in CORE_CATEGORIES]
    missing = (scores["universe_flag"] == 1) & (scores[core].notna().sum(axis=1) < 3)
    counts["CORE_FACTOR_MISSING"] = int(missing.sum())
    return dict(sorted(counts.items()))


def _candidate(
    code: str,
    row: pd.Series,
    rank: int,
    profile_score: float,
    data: dict[str, Any],
) -> dict[str, Any]:
    # 组合候选股票的因子解释和当前关键指标。
    universe = data["universe"]
    snapshot = universe.loc[code]
    bars = data["bars"].loc[data["bars"]["stock_code"] == code]
    latest_bar = bars.sort_values("trade_date").iloc[-1]
    financial = data["financial"].reindex([code]).iloc[0]
    strengths, risks = _factor_summary(row)
    pe = _finite(snapshot.get("pe_ttm"), 2)
    if pe is not None and pe > 80:
        risks.append("市盈率高于80倍")
    day_return = _finite(latest_bar.get("pct_change"), 2)
    factors = {
        name: _finite(row.get(f"{name}_score"), 3)
        for name in CATEGORY_NAMES
    }
    detail = {
        "code": code,
        "name": snapshot.get("stock_name") or code,
        "industry": snapshot.get("industry_name") or "未分类",
        "rank": rank,
        "score": _finite(profile_score, 4),
        "strengths": strengths,
        "risks": risks,
        "metrics": {
            "close": _finite(latest_bar.get("close"), 2),
            "day_return": day_return,
            "month_return": _period_return(bars, 20),
            "year_return": _period_return(bars, 250),
            "pe": pe,
            "pb": _finite(snapshot.get("pb_mrq"), 2),
            "roe": _finite(financial.get("roe_pct"), 2),
            "revenue_growth": _finite(financial.get("revenue_yoy") * 100, 2),
            "amount_10k": _finite(latest_bar.get("amount_10k"), 2),
            "turnover": _finite(snapshot.get("turnover_rate"), 2),
        },
        "factors": factors,
        "factor_detail": row.get("factor_detail_json"),
        "financial_report_date": financial.get("report_date"),
        "financial_announce_date": financial.get("announce_date"),
    }
    return detail


def generate(market_db: str | Path, progress=None) -> dict[str, Any]:
    # 一次加载数据并生成三种风险类型的当前 Top 20。
    connection = connect_market(market_db)
    try:
        factor_date = latest_complete_date(connection)
        if progress:
            progress("正在读取行情、财务与股票池…")
        config = load_config()
        data = load_factor_data(connection, factor_date, config)
        if progress:
            progress("正在计算七类因子…")
        scores = calculate_scores(data, config)
        financial_dates = data["financial"].get("report_date", pd.Series(dtype=str))
        summary = {
            "factor_date": factor_date,
            "financial_report_date": (
                str(financial_dates.dropna().max()) if not financial_dates.empty else None
            ),
            "model_version": config["model_version"],
            "universe_count": int(len(scores)),
            "eligible_count": int((scores["universe_flag"] == 1).sum()),
            "exclusions": _exclusion_counts(scores),
        }
        results: dict[str, Any] = {}
        for profile, weights in PROFILE_WEIGHTS.items():
            if progress:
                progress(f"正在生成{PROFILE_NAMES[profile]}候选…")
            values = _profile_score(scores, weights)
            ranked = values.dropna().sort_values(ascending=False, kind="mergesort")
            ranked = ranked.sort_index(kind="mergesort").sort_values(
                ascending=False,
                kind="mergesort",
            )
            items = [
                _candidate(code, scores.loc[code], rank, value, data)
                for rank, (code, value) in enumerate(ranked.head(30).items(), 1)
            ]
            results[profile] = {
                "profile": profile,
                "profile_name": PROFILE_NAMES[profile],
                "weights": weights,
                "candidate_count": len(items),
                "scored_count": int(ranked.size),
                "items": items,
            }
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "summary": summary,
            "profiles": results,
        }
    finally:
        connection.close()
