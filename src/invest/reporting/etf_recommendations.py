"""Generate current exploratory ETF research recommendations in memory."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROFILE_WEIGHTS = {
    "conservative": {"momentum": 0.20, "trend": 0.20, "low_risk": 0.40, "liquidity": 0.20},
    "balanced": {"momentum": 0.35, "trend": 0.30, "low_risk": 0.20, "liquidity": 0.15},
    "aggressive": {"momentum": 0.55, "trend": 0.30, "low_risk": 0.05, "liquidity": 0.10},
}

PROFILE_NAMES = {"conservative": "稳健型", "balanced": "均衡型", "aggressive": "进取型"}
CATEGORY_NAMES = {"momentum": "动量", "trend": "趋势", "low_risk": "低风险", "liquidity": "流动性"}
LOOKBACK_DAYS = 300
TOP_COUNT = 30


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    # 以只读 URI 连接包含 ETF 数据的本地市场库。
    target = Path(path).resolve()
    if not target.is_file() or not target.stat().st_size:
        raise FileNotFoundError(f"市场数据库不存在或为空: {target}")
    connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _finite(value: Any, digits: int = 4) -> float | None:
    # 将数值转换为 JSON 可安全输出的有限小数。
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def _recent_dates(connection: sqlite3.Connection) -> list[str]:
    # 获取最近完整行情日及其前的固定观察窗口。
    rows = connection.execute(
        """
        SELECT DISTINCT trade_date FROM etf_daily
        WHERE close>0 ORDER BY trade_date DESC LIMIT ?
        """,
        (LOOKBACK_DAYS,),
    ).fetchall()
    dates = sorted(str(row[0]) for row in rows)
    if len(dates) < 253:
        raise ValueError("ETF 行情历史不足 253 个交易日")
    return dates


def _read_data(
    connection: sqlite3.Connection,
    dates: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, set[str]]:
    # 读取主数据、观察窗口日线和已记录的公司行动日期。
    master = pd.read_sql_query("SELECT * FROM etf_master", connection)
    marks = ",".join("?" for _ in dates)
    bars = pd.read_sql_query(
        "SELECT etf_code,trade_date,close,amount_10k,is_trading,forward_factor "
        f"FROM etf_daily WHERE trade_date IN ({marks}) ORDER BY etf_code,trade_date",
        connection,
        params=dates,
    )
    actions = set()
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='etf_action'"
    ).fetchone()
    if exists:
        actions = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT ex_date FROM etf_action WHERE verified=1"
            )
        }
    return master, bars, actions


def _winsor_rank(values: pd.Series) -> pd.Series:
    # 进行 MAD 三倍缩尾并返回 [-1, 1] 横截面秩分数。
    numbers = pd.to_numeric(values, errors="coerce").astype(float)
    numbers = numbers.where(np.isfinite(numbers))
    median = numbers.median()
    mad = (numbers - median).abs().median()
    if pd.notna(median) and pd.notna(mad) and mad > 0:
        numbers = numbers.clip(median - 3 * 1.4826 * mad, median + 3 * 1.4826 * mad)
    count = int(numbers.notna().sum())
    if count <= 1:
        return numbers * 0.0
    return numbers.rank(method="average", pct=True) * 2 - 1


def _return(prices: pd.Series, days: int) -> float:
    # 计算指定交易日长度的未复权收盘价收益率。
    valid = prices.dropna()
    if len(valid) <= days or valid.iloc[-days - 1] <= 0:
        return np.nan
    return float(valid.iloc[-1] / valid.iloc[-days - 1] - 1)


def _max_drawdown(prices: pd.Series) -> float:
    # 计算指定价格序列的最大回撤。
    valid = prices.dropna()
    if valid.empty:
        return np.nan
    return float((valid / valid.cummax() - 1).min())


def _explained_jump(group: pd.DataFrame, action_dates: set[str]) -> bool:
    # 判断观察期内是否存在未被公司行动或复权因子解释的异常跳变。
    ordered = group.sort_values("trade_date").copy()
    close = ordered["close"].where(ordered["close"] > 0)
    raw_jump = close.pct_change(fill_method=None).abs().ge(0.40)
    if not raw_jump.any():
        return False
    factor = ordered["forward_factor"].where(ordered["forward_factor"] > 0)
    adjusted = (close * factor).pct_change(fill_method=None).abs().lt(0.40)
    for index in ordered.index[raw_jump]:
        date = str(ordered.at[index, "trade_date"])
        is_adjusted = adjusted.get(index, False)
        if date not in action_dates and not (pd.notna(is_adjusted) and bool(is_adjusted)):
            return True
    return False


def _feature_row(group: pd.DataFrame, action_dates: set[str]) -> dict[str, Any]:
    # 为一只 ETF 计算准入指标、原始特征和数据质量状态。
    ordered = group.sort_values("trade_date").copy()
    valid = (
        ordered["close"].gt(0)
        & ordered["amount_10k"].gt(0)
        & ordered["is_trading"].eq(1)
    )
    prices = ordered["close"].where(valid)
    recent_253 = valid.tail(253)
    recent_20 = valid.tail(20)
    amount = ordered["amount_10k"].where(valid)
    returns = prices.pct_change(fill_method=None)
    recent_returns = returns.tail(60).dropna()
    downside = recent_returns.loc[recent_returns < 0]
    adv20 = amount.tail(20).mean()
    adv60 = amount.tail(60).mean()
    amount_cv = amount.tail(20).std() / adv20 if adv20 and adv20 > 0 else np.nan
    ma = {days: prices.tail(days).mean() for days in (20, 60, 200)}
    close = prices.iloc[-1] if len(prices) else np.nan
    reasons = []
    if not bool(valid.iloc[-1]):
        reasons.append("INVALID_LATEST_DAY")
    if int(valid.sum()) < 253:
        reasons.append("INSUFFICIENT_HISTORY")
    if recent_253.mean() < 0.95:
        reasons.append("POOR_COVERAGE")
    if int(recent_20.sum()) < 18:
        reasons.append("INSUFFICIENT_RECENT_TRADING")
    if pd.isna(adv20) or adv20 < 2000:
        reasons.append("LOW_LIQUIDITY")
    if _explained_jump(ordered.tail(253), action_dates):
        reasons.append("UNEXPLAINED_PRICE_JUMP")
    return {
        "etf_code": str(ordered.iloc[0]["etf_code"]),
        "eligible": not reasons,
        "reasons": reasons,
        "close": close,
        "day_return": _return(prices, 1),
        "return_20": _return(prices, 20),
        "return_60": _return(prices, 60),
        "return_126": _return(prices, 126),
        "return_252": _return(prices, 252),
        "trend_20": close / ma[20] - 1 if ma[20] and ma[20] > 0 else np.nan,
        "trend_60": close / ma[60] - 1 if ma[60] and ma[60] > 0 else np.nan,
        "trend_200": close / ma[200] - 1 if ma[200] and ma[200] > 0 else np.nan,
        "volatility_60": recent_returns.std() * np.sqrt(252),
        "downside_volatility_60": downside.std() * np.sqrt(252),
        "max_drawdown_120": _max_drawdown(prices.tail(120)),
        "adv20_10k": adv20,
        "adv60_10k": adv60,
        "coverage_253": recent_253.mean(),
        "amount_cv_20": amount_cv,
    }


def _score_features(frame: pd.DataFrame) -> pd.DataFrame:
    # 由标准化子指标合成四类 ETF 研究因子。
    result = frame.copy()
    result["momentum_score"] = (
        0.15 * _winsor_rank(result["return_20"])
        + 0.20 * _winsor_rank(result["return_60"])
        + 0.30 * _winsor_rank(result["return_126"])
        + 0.35 * _winsor_rank(result["return_252"])
    )
    result["trend_score"] = (
        0.20 * _winsor_rank(result["trend_20"])
        + 0.30 * _winsor_rank(result["trend_60"])
        + 0.50 * _winsor_rank(result["trend_200"])
    )
    result["low_risk_score"] = (
        0.40 * _winsor_rank(-result["volatility_60"])
        + 0.25 * _winsor_rank(-result["downside_volatility_60"])
        + 0.35 * _winsor_rank(result["max_drawdown_120"])
    )
    result["liquidity_score"] = (
        0.50 * _winsor_rank(np.log(result["adv20_10k"]))
        + 0.25 * _winsor_rank(result["coverage_253"])
        + 0.25 * _winsor_rank(-result["amount_cv_20"])
    )
    return result


def _profile_score(frame: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    # 使用固定类型权重合成完整四类因子的 ETF 综合分。
    columns = [f"{name}_score" for name in weights]
    values = frame[columns]
    series = sum(values[column] * weights[column.removesuffix("_score")] for column in columns)
    return series.where(values.notna().all(axis=1) & frame["eligible"])


def _exclusion_counts(frame: pd.DataFrame) -> dict[str, int]:
    # 汇总准入阶段排除原因，供页面显示数据覆盖情况。
    counts: Counter[str] = Counter()
    for reasons in frame["reasons"]:
        counts.update(reasons)
    return dict(sorted(counts.items()))


def _summaries(row: pd.Series) -> tuple[list[str], list[str]]:
    # 根据四类真实得分和原始指标生成优势与风险说明。
    values = {name: row[f"{name}_score"] for name in CATEGORY_NAMES}
    ordered = sorted(values, key=lambda name: (-float(values[name]), name))
    strengths = [f"{CATEGORY_NAMES[name]}较强" for name in ordered[:2]]
    weak = sorted(values, key=lambda name: (float(values[name]), name))[:2]
    risks = [f"{CATEGORY_NAMES[name]}相对偏弱" for name in weak]
    if row["trend_200"] < 0:
        risks.append("低于 200 日均线")
    if pd.isna(row["underlying_code"]) or not row["underlying_code"]:
        risks.append("跟踪指数代码缺失")
    risks.append("复权与公司行动尚未完整核验")
    return strengths, risks


def _candidate(row: pd.Series, rank: int, score: float) -> dict[str, Any]:
    # 序列化页面表格和详情侧栏所需的一只 ETF 研究候选。
    strengths, risks = _summaries(row)
    factors = {name: _finite(row[f"{name}_score"], 3) for name in CATEGORY_NAMES}
    metrics = {
        "close": _finite(row["close"], 3),
        "day_return": _finite(100 * row["day_return"], 2),
        "return_20": _finite(100 * row["return_20"], 2),
        "return_60": _finite(100 * row["return_60"], 2),
        "return_126": _finite(100 * row["return_126"], 2),
        "return_252": _finite(100 * row["return_252"], 2),
        "volatility_60": _finite(100 * row["volatility_60"], 2),
        "downside_volatility_60": _finite(100 * row["downside_volatility_60"], 2),
        "max_drawdown_120": _finite(100 * row["max_drawdown_120"], 2),
        "adv20_10k": _finite(row["adv20_10k"], 2),
        "adv60_10k": _finite(row["adv60_10k"], 2),
        "coverage_253": _finite(100 * row["coverage_253"], 2),
    }
    return {
        "code": row["etf_code"],
        "name": row["etf_name"] or row["etf_code"],
        "underlying_code": row["underlying_code"] or None,
        "list_date": row["list_date"] or None,
        "rank": rank,
        "score": _finite(score, 4),
        "strengths": strengths,
        "risks": risks,
        "factors": factors,
        "metrics": metrics,
        "data_quality": "探索性：复权、基金净值和公司行动证据尚未完整核验",
    }


def generate(market_db: str | Path, progress=None) -> dict[str, Any]:
    # 计算全市场探索性 ETF 候选，结果只返回给调用方保存于内存。
    connection = connect_readonly(market_db)
    try:
        if progress:
            progress("正在读取 ETF 行情与产品清单…")
        dates = _recent_dates(connection)
        master, bars, actions = _read_data(connection, dates)
        if progress:
            progress("正在检查流动性、覆盖率和异常价格…")
        rows = [_feature_row(group, actions) for _, group in bars.groupby("etf_code")]
        features = pd.DataFrame(rows).merge(master, on="etf_code", how="left")
        exclusions = _exclusion_counts(features)
        eligible = features.loc[features["eligible"]].copy()
        eligible["dedup_key"] = eligible["underlying_code"].fillna(eligible["etf_code"])
        eligible = eligible.sort_values(
            ["dedup_key", "adv60_10k", "etf_code"],
            ascending=[True, False, True],
        )
        deduped = eligible.drop_duplicates("dedup_key", keep="first").copy()
        if progress:
            progress("正在计算动量、趋势、风险和流动性评分…")
        scored = _score_features(deduped)
        results: dict[str, Any] = {}
        for profile, weights in PROFILE_WEIGHTS.items():
            if progress:
                progress(f"正在生成{PROFILE_NAMES[profile]} ETF 候选…")
            values = _profile_score(scored, weights)
            ranked = values.dropna().sort_index(kind="mergesort").sort_values(
                ascending=False,
                kind="mergesort",
            )
            items = [
                _candidate(scored.loc[code], rank, score)
                for rank, (code, score) in enumerate(ranked.head(TOP_COUNT).items(), 1)
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
            "summary": {
                "factor_date": dates[-1],
                "universe_count": int(len(master)),
                "eligible_count": int(len(eligible)),
                "deduped_count": int(len(deduped)),
                "exclusions": exclusions,
                "quality_level": "exploratory",
                "warning": "分类、复权、净值和公司行动证据尚未完整核验。",
            },
            "profiles": results,
        }
    finally:
        connection.close()
