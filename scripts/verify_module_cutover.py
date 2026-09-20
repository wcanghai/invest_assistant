"""使用固定合成输入比较迁移前后的全部核心计算；旧代码仅从显式备份加载。"""

import argparse
import importlib
import json
import math
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def compare(left, right, path="result"):
    # 对结构和离散值精确比较，仅对浮点使用约定容差并保持缺失位置。
    if isinstance(left, pd.DataFrame):
        pd.testing.assert_frame_equal(left, right, rtol=1e-10, atol=1e-12)
    elif isinstance(left, pd.Series):
        pd.testing.assert_series_equal(left, right, rtol=1e-10, atol=1e-12)
    elif isinstance(left, dict):
        assert left.keys() == right.keys(), path
        for key in left:
            compare(left[key], right[key], f"{path}.{key}")
    elif isinstance(left, (list, tuple, np.ndarray)):
        assert len(left) == len(right), path
        for index, (a, b) in enumerate(zip(left, right)):
            compare(a, b, f"{path}[{index}]")
    elif isinstance(left, (float, np.floating)):
        if math.isnan(left):
            assert math.isnan(right), path
        else:
            assert math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-12), path
    else:
        assert left == right, path


def module(name):
    # 明确导入旧包或新包，不建立生产兼容入口。
    return importlib.import_module(name)


def main():
    # 合成数据和输出限定到显式目录；完全不打开正式数据库。
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(args.legacy_root.resolve()))
    args.output.mkdir(parents=True, exist_ok=True)
    from tests.unit.test_factor_strategy import _seed_database
    from tests.unit.test_etf_strategy import market
    from tests.unit.test_etf_rotation import RotationTests

    checks = []
    old, new = "factor_strategy", "invest.research.stocks"
    config = module(new + ".config").load_config()
    with closing(_seed_database()) as connection:
        compare(module(old + ".main").score_date(connection, "2026-08-31", config),
                module(new + ".main").score_date(connection, "2026-08-31", config))
        checks.append("stock_factor_scores")
        data = module(new + ".data").load_factor_data(connection, "2026-08-31", config)
        for strategy in module(new + ".mvp").STRATEGIES:
            left = module(old + ".mvp").score_strategy(data, strategy)
            right = module(new + ".mvp").score_strategy(data, strategy)
            compare(left, right)
            compare(module(old + ".mvp").build_equal_weight_target(left),
                    module(new + ".mvp").build_equal_weight_target(right))
            checks.append("stock_mvp_" + strategy)
        left = module(old + ".backtest").run_backtest(
            connection, "2026-06-01", "2026-08-31", config)
        right = module(new + ".backtest").run_backtest(
            connection, "2026-06-01", "2026-08-31", config)
        for name in ("nav", "trades"):
            compare(left[name].drop(columns=["backtest_id", "order_id"], errors="ignore"),
                    right[name].drop(columns=["backtest_id", "order_id"], errors="ignore"))
            checks.append("stock_backtest_" + name)
    path, dates = market.__wrapped__(args.output)
    old, new = "etf_strategy", "invest.research.etf"
    config = module(new + ".config").load_config()
    left_data = module(old + ".data").Dataset(path, config)
    right_data = module(new + ".data").Dataset(path, config)
    compare(left_data.universe(dates[300]), right_data.universe(dates[300]))
    checks.append("etf_universe")
    for strategy in module(new + ".config").STRATEGIES:
        compare(module(old + ".strategies").target(left_data, dates[300], strategy),
                module(new + ".strategies").target(right_data, dates[300], strategy))
        compare(module(old + ".backtest").backtest(left_data, strategy, dates[250], dates[-1]),
                module(new + ".backtest").backtest(right_data, strategy, dates[250], dates[-1]))
        checks.append("etf_signal_and_backtest_" + strategy)
    case = RotationTests()
    case.setUp()
    old = module("factor_strategy.etf_rotation")
    new = module("invest.research.etf.three_momentum")
    scores, details = old.make_scores(case.bars, case.config)
    compare((scores, details), new.make_scores(case.bars, case.config))
    compare(old.backtest(case.bars, scores, case.config),
            new.backtest(case.bars, scores, case.config))
    checks.append("etf_three_momentum_scores_nav_trades")
    result = {"status": "passed", "rtol": 1e-10, "atol": 1e-12, "checks": checks,
              "legacy_root": str(args.legacy_root.resolve()),
              "ignored": ["stock.backtest_id", "stock.order_id"]}
    (args.output / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
