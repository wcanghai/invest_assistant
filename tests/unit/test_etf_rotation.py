"""ETF 轮动的阈值、时间因果性和成交成本验证。"""

import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from invest.research.etf.three_momentum import backtest
from invest.research.etf.three_momentum import choose_target
from invest.research.etf.three_momentum import make_scores
from invest.research.etf.three_momentum import regression


class RotationTests(unittest.TestCase):
    def setUp(self):
        # 加载默认配置并构造两个资产的连续有效测试行情。
        self.config = json.loads(Path("configs/etf_three_momentum.json").read_text())
        self.config.update(codes=["A", "B"], start="2020-01-01")
        dates = pd.date_range("2020-01-01", periods=120).strftime("%Y-%m-%d")
        self.bars = {}
        for code, rate in (("A", 0.002), ("B", 0.001)):
            prices = np.exp(np.arange(120) * rate)
            self.bars[code] = pd.DataFrame(
                {name: prices for name in ("open", "high", "low", "close")}, index=dates
            )

    def test_constant_regression(self):
        # 常数价格没有趋势。
        self.assertEqual(regression([1, 1, 1]), (0, 0))

    def test_negative_threshold_and_missing(self):
        # 负分改善不足时不换仓，缺失因子时不凭空选择资产。
        scores = pd.Series({"A": -1.0, "B": -0.8})
        self.assertEqual(choose_target(scores, "A", self.config), "A")
        self.config["threshold_mode"] = "literal"
        self.assertEqual(choose_target(scores, "A", self.config), "B")
        scores["B"] = np.nan
        self.assertEqual(choose_target(scores, "A", self.config), "A")

    def test_future_prices_cannot_change_past(self):
        # 修改未来行情不得影响历史因子或排名。
        before, _ = make_scores(self.bars, self.config)
        self.bars["A"].iloc[100:] *= 2
        after, _ = make_scores(self.bars, self.config)
        pd.testing.assert_frame_equal(before.iloc[:100], after.iloc[:100])
        self.assertTrue(before.iloc[:83].isna().all().all())
        self.assertTrue(before.iloc[83:].notna().all().all())

    def test_next_open_and_cost(self):
        # 首日信号不可当日成交，手续费和滑点使初始买入净值降低。
        scores = pd.DataFrame({"A": 1.0, "B": -1.0}, index=self.bars["A"].index)
        curve, trades, _ = backtest(self.bars, scores, self.config)
        self.assertEqual(trades.iloc[0]["date"], scores.index[1])
        self.assertEqual(trades.iloc[0]["signal_date"], scores.index[0])
        self.assertEqual(curve.iloc[0].equity, self.config["initial_cash"])
        self.assertLess(curve.iloc[1].equity, curve.iloc[0].equity)
        self.assertTrue(curve.cash.ge(0).all())

    def test_suspended_destination(self):
        # 目标缺失开盘行情时保留现金，恢复后使用前日信号买入。
        scores = pd.DataFrame({"A": 1.0, "B": -1.0}, index=self.bars["A"].index)
        self.bars["A"].iloc[1] = np.nan
        _, trades, _ = backtest(self.bars, scores, self.config)
        self.assertEqual(trades.iloc[0]["date"], scores.index[2])


if __name__ == "__main__":
    unittest.main()
