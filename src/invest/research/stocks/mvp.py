"""四种典型日频量化策略的轻量 MVP。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from invest.core.settings import load_settings

import numpy as np
import pandas as pd

from invest.research.stocks.config import load_config
from invest.research.stocks.data import load_factor_data


STRATEGIES = ("momentum", "low_vol", "reversal", "quality_value")


def _period_return(prices: pd.Series, start_offset: int, end_offset: int = 0) -> float:
    # 按交易日位置计算区间复权收益率。
    values = pd.to_numeric(prices, errors="coerce").dropna()
    if len(values) <= start_offset or values.iloc[-start_offset - 1] <= 0:
        return np.nan
    start = values.iloc[-start_offset - 1]
    end = values.iloc[-end_offset - 1]
    return float(end / start - 1.0)


def _maximum_drawdown(prices: pd.Series, window: int) -> float:
    # 返回窗口内最大回撤，数值越接近零越好。
    values = pd.to_numeric(prices, errors="coerce").dropna().tail(window)
    if len(values) < window // 2:
        return np.nan
    return float((values / values.cummax() - 1.0).min())


def _momentum_signals(data: dict) -> tuple[pd.DataFrame, dict[str, float], int]:
    # 计算跳过最近一个月的中期价格动量。
    rows = []
    for stock_code, group in data["bars"].groupby("stock_code"):
        prices = group.sort_values("trade_date")["adj_close"]
        rows.append({
            "stock_code": stock_code,
            "momentum_12_1": _period_return(prices, 252, 21),
            "momentum_6_1": _period_return(prices, 126, 21),
            "momentum_3_1": _period_return(prices, 63, 21),
        })
    weights = {"momentum_12_1": 0.50, "momentum_6_1": 0.30, "momentum_3_1": 0.20}
    return _rows_to_frame(rows), weights, 2


def _low_vol_signals(data: dict) -> tuple[pd.DataFrame, dict[str, float], int]:
    # 计算低波动、低下行波动和低回撤信号。
    rows = []
    for stock_code, group in data["bars"].groupby("stock_code"):
        ordered = group.sort_values("trade_date")
        returns = pd.to_numeric(ordered["return"], errors="coerce").dropna()
        recent = returns.tail(60)
        downside = recent.loc[recent < 0]
        rows.append({
            "stock_code": stock_code,
            "low_volatility": -recent.std() * np.sqrt(252),
            "low_downside_volatility": -downside.std() * np.sqrt(252),
            "low_drawdown": _maximum_drawdown(ordered["adj_close"], 120),
        })
    weights = {
        "low_volatility": 0.40,
        "low_downside_volatility": 0.30,
        "low_drawdown": 0.30,
    }
    return _rows_to_frame(rows), weights, 2


def _reversal_signals(data: dict) -> tuple[pd.DataFrame, dict[str, float], int]:
    # 计算五日和二十日短期反转信号。
    rows = []
    for stock_code, group in data["bars"].groupby("stock_code"):
        prices = group.sort_values("trade_date")["adj_close"]
        rows.append({
            "stock_code": stock_code,
            "reversal_5d": -_period_return(prices, 5),
            "reversal_20d": -_period_return(prices, 20),
        })
    weights = {"reversal_5d": 0.70, "reversal_20d": 0.30}
    return _rows_to_frame(rows), weights, 2


def _positive_inverse(values: pd.Series) -> pd.Series:
    # 将正估值倍数转换为收益率口径，非正值视为缺失。
    numeric = pd.to_numeric(values, errors="coerce")
    return 1.0 / numeric.where(numeric > 0)


def _quality_value_signals(data: dict) -> tuple[pd.DataFrame, dict[str, float], int]:
    # 用历史快照估值和已公告财务数据构造价值质量信号。
    universe = data["universe"]
    financial = data["financial"].reindex(universe.index)
    result = pd.DataFrame(index=universe.index)
    result["earnings_yield"] = _positive_inverse(universe["pe_ttm"])
    result["book_to_price"] = _positive_inverse(universe["pb_mrq"])
    result["dividend_yield"] = pd.to_numeric(
        universe["dividend_yield"], errors="coerce"
    ).where(lambda values: values >= 0)
    result["roe"] = pd.to_numeric(financial["roe_pct"], errors="coerce")
    result["gross_margin"] = pd.to_numeric(
        financial["gross_margin_pct"], errors="coerce"
    )
    result["low_debt"] = -pd.to_numeric(financial["debt_ratio_pct"], errors="coerce")
    result["revenue_growth"] = financial["revenue_yoy"]
    result["profit_growth"] = financial["parent_profit_yoy"]
    weights = {
        "earnings_yield": 0.20,
        "book_to_price": 0.15,
        "dividend_yield": 0.10,
        "roe": 0.15,
        "gross_margin": 0.10,
        "low_debt": 0.10,
        "revenue_growth": 0.10,
        "profit_growth": 0.10,
    }
    return result, weights, 4


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    # 将按股票生成的信号记录转换为股票代码索引表。
    if not rows:
        return pd.DataFrame(index=pd.Index([], name="stock_code"))
    return pd.DataFrame(rows).set_index("stock_code")


def _rank_composite(
    signals: pd.DataFrame,
    weights: dict[str, float],
    minimum_features: int,
) -> pd.DataFrame:
    # 对各信号做百分位排名并按可用权重合成总分。
    ranked = signals[list(weights)].rank(pct=True, method="average")
    weight_series = pd.Series(weights, dtype=float)
    available = ranked.notna()
    denominator = available.mul(weight_series, axis=1).sum(axis=1)
    numerator = ranked.mul(weight_series, axis=1).sum(axis=1, min_count=1)
    result = signals.copy()
    result["valid_feature_count"] = available.sum(axis=1)
    result["score"] = numerator / denominator.where(denominator > 0)
    result.loc[result["valid_feature_count"] < minimum_features, "score"] = np.nan
    return result


def score_strategy(data: dict, strategy: str) -> pd.DataFrame:
    # 为指定策略生成完整股票截面得分。
    calculators: dict[
        str,
        Callable[[dict], tuple[pd.DataFrame, dict[str, float], int]],
    ] = {
        "momentum": _momentum_signals,
        "low_vol": _low_vol_signals,
        "reversal": _reversal_signals,
        "quality_value": _quality_value_signals,
    }
    if strategy not in calculators:
        raise ValueError(f"未知策略：{strategy}")
    universe = data["universe"]
    signals, weights, minimum = calculators[strategy](data)
    eligible = universe.index[universe["universe_flag"] == 1]
    scored = _rank_composite(signals.reindex(eligible), weights, minimum)
    metadata = universe.reindex(scored.index)[
        ["stock_name", "industry_code", "industry_name", "close", "adv_20"]
    ]
    result = metadata.join(scored)
    result.index.name = "stock_code"
    return result.sort_values("score", ascending=False)


def build_equal_weight_target(
    scores: pd.DataFrame,
    top_n: int = 20,
    max_industry_fraction: float = 0.20,
) -> pd.DataFrame:
    # 从高分股票中生成带行业数量上限的等权目标组合。
    if top_n <= 0:
        raise ValueError("top_n 必须大于零")
    maximum = max(1, int(np.floor(top_n * max_industry_fraction)))
    selected = []
    industry_counts: dict[str, int] = {}
    for stock_code, row in scores.loc[scores["score"].notna()].iterrows():
        industry = str(row.get("industry_code") or "UNKNOWN")
        if industry_counts.get(industry, 0) >= maximum:
            continue
        selected.append(stock_code)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(selected) == top_n:
            break
    target = scores.loc[selected].copy()
    if target.empty:
        target["rank_no"] = pd.Series(dtype=int)
        target["target_weight"] = pd.Series(dtype=float)
        return target.reset_index()
    target["rank_no"] = np.arange(1, len(target) + 1)
    target["target_weight"] = 1.0 / len(target)
    return target.reset_index()


def _open_read_only(path: Path) -> sqlite3.Connection:
    # 以只读模式打开策略数据库。
    resolved = path.resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _latest_snapshot_date(connection: sqlite3.Connection) -> str:
    # 返回数据库中最新证券池快照日期。
    row = connection.execute("SELECT MAX(snapshot_date) FROM stock_snapshot").fetchone()
    if row is None or row[0] is None:
        raise ValueError("数据库中没有证券池快照")
    return str(row[0])


def _parse_args() -> argparse.Namespace:
    # 解析轻量策略命令行参数。
    parser = argparse.ArgumentParser(description="典型量化策略 MVP")
    parser.add_argument("--db", default=str(load_settings().databases["market"]))
    parser.add_argument("--date", help="信号日，默认使用最新证券池快照")
    parser.add_argument("--strategy", choices=(*STRATEGIES, "all"), default="all")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-industry-fraction", type=float, default=0.20)
    parser.add_argument("--output-dir", help="可选的 CSV 输出目录")
    return parser.parse_args()


def main() -> None:
    # 加载一次数据并运行一个或全部 MVP 策略。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parse_args()
    connection = _open_read_only(Path(args.db))
    try:
        factor_date = args.date or _latest_snapshot_date(connection)
        data = load_factor_data(connection, factor_date, load_config())
    finally:
        connection.close()
    strategy_names = STRATEGIES if args.strategy == "all" else (args.strategy,)
    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    for strategy in strategy_names:
        scores = score_strategy(data, strategy)
        target = build_equal_weight_target(
            scores,
            top_n=args.top,
            max_industry_fraction=args.max_industry_fraction,
        )
        display_columns = [
            "rank_no",
            "stock_code",
            "stock_name",
            "industry_name",
            "score",
            "target_weight",
        ]
        print(f"\n[{factor_date} {strategy}]")
        print(target[display_columns].to_string(index=False))
        if output_dir is not None:
            output_path = output_dir / f"{strategy}_{factor_date}.csv"
            target.to_csv(output_path, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
