"""简化版多因子策略命令行入口。"""

from __future__ import annotations

from invest.core.settings import load_settings

import argparse
import json
import uuid
from pathlib import Path

from invest.storage import factor as db
from invest.research.stocks.backtest import run_backtest
from invest.research.stocks.config import config_hash
from invest.research.stocks.config import load_config
from invest.research.stocks.data import load_factor_data
from invest.research.stocks.portfolio import build_portfolio
from invest.research.stocks.scoring import calculate_scores


DEFAULT_DB_PATH = load_settings().databases["research"]


def score_date(connection, factor_date: str, config: dict):
    # 计算一个交易日的完整因子截面。
    data = load_factor_data(connection, factor_date, config)
    return calculate_scores(data, config)


def save_scores(connection, scores, factor_date: str, config: dict, run_id: str) -> int:
    # 将评分 DataFrame 转换为数据库记录并保存。
    rows = []
    for stock_code, row in scores.iterrows():
        rows.append({
            "factor_date": factor_date,
            "stock_code": stock_code,
            "model_version": config["model_version"],
            "universe_flag": int(row["universe_flag"]),
            "exclusion_reason": row["exclusion_reason"],
            "value_score": row["value_score"],
            "quality_score": row["quality_score"],
            "growth_score": row["growth_score"],
            "momentum_score": row["momentum_score"],
            "low_risk_score": row["low_risk_score"],
            "flow_sentiment_score": row["flow_sentiment_score"],
            "liquidity_score": row["liquidity_score"],
            "total_score": row["total_score"],
            "valid_factor_count": int(row["valid_factor_count"]),
            "factor_detail_json": row["factor_detail_json"],
            "run_id": run_id,
        })
    return db.upsert_rows(
        connection,
        "factor_score_daily",
        rows,
        ("factor_date", "stock_code", "model_version"),
    )


def _build_parser() -> argparse.ArgumentParser:
    # 构建策略命令行参数。
    parser = argparse.ArgumentParser(description="A 股多因子策略")
    parser.add_argument("command", choices=("init-db", "score", "portfolio", "backtest"))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--market-db", default=str(load_settings().databases["market"]))
    parser.add_argument("--config")
    parser.add_argument("--date")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    # 执行建表、单日评分、组合生成或历史回测。
    args = _build_parser().parse_args()
    config = load_config(args.config)
    connection = db.connect(args.db, args.market_db)
    try:
        db.init_strategy_schema(connection)
        if args.command == "init-db":
            print("策略数据表初始化完成")
            return
        if args.command in {"score", "portfolio"}:
            if not args.date:
                raise ValueError("score 和 portfolio 必须提供 --date")
            run_id = uuid.uuid4().hex
            scores = score_date(connection, args.date, config)
            save_scores(connection, scores, args.date, config, run_id)
            if args.output:
                args.output.mkdir(parents=True, exist_ok=True)
                scores.to_csv(args.output / "scores.csv", encoding="utf-8-sig")
            if args.command == "score":
                print(scores.sort_values("total_score", ascending=False).head(20))
                return
            target = build_portfolio(scores, None, config)
            rows = target.assign(
                rebalance_date=args.date,
                model_version=config["model_version"],
                run_id=run_id,
            ).to_dict("records")
            db.upsert_rows(
                connection,
                "portfolio_target",
                rows,
                ("rebalance_date", "stock_code", "model_version"),
            )
            print(target)
            if args.output:
                target.to_csv(args.output / "portfolio.csv", index=False, encoding="utf-8-sig")
            return
        if not args.start or not args.end:
            raise ValueError("backtest 必须提供 --start 和 --end")
        result = run_backtest(connection, args.start, args.end, config)
        nav_rows = result["nav"].to_dict("records")
        trade_rows = result["trades"].to_dict("records")
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            result["nav"].to_csv(args.output / "nav.csv", index=False)
            result["trades"].to_csv(args.output / "trades.csv", index=False)
        db.upsert_rows(connection, "backtest_nav", nav_rows, ("backtest_id", "trade_date"))
        db.upsert_rows(connection, "backtest_trade", trade_rows, ("backtest_id", "order_id"))
        summary = {
            "backtest_id": result["backtest_id"],
            "days": len(nav_rows),
            "trades": len(trade_rows),
            "final_nav": nav_rows[-1]["nav"] if nav_rows else None,
            "config_hash": config_hash(config),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
