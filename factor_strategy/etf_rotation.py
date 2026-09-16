"""微信三因子 ETF 动量轮动的可复现日频研究实现。"""

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd


def regression(values):
    # 计算带截距线性回归的斜率和拟合优度，常数序列返回零斜率。
    y = np.asarray(values, dtype=float)
    x = np.arange(len(y), dtype=float)
    slope = np.dot(x - x.mean(), y - y.mean()) / np.sum((x - x.mean()) ** 2)
    total = np.sum((y - y.mean()) ** 2)
    error = np.sum((y - y.mean() - slope * (x - x.mean())) ** 2)
    return slope, max(0.0, 1 - error / total) if total > 0 else 0.0


def make_scores(bars, config):
    # 只使用截至当日的连续有效行情计算因子并做四资产截面标准化。
    factors = {}
    for code in config["codes"]:
        frame = bars[code]
        close = frame["close"]
        bias = close / close.rolling(config["bias_window"]).mean()
        bias_factor = bias.rolling(config["momentum_window"]).apply(
            lambda y: regression(y / y[0])[0] * 10000, raw=True
        )
        slope_factor = close.rolling(config["slope_window"]).apply(
            lambda y: np.prod(regression(y / y[0])) * 10000, raw=True
        )
        pivot = frame[["open", "high", "low", "close"]].mean(axis=1, skipna=False)
        log_pivot = np.log(pivot)
        window = config["efficiency_window"]
        change = log_pivot - log_pivot.shift(window - 1)
        distance = log_pivot.diff().abs().rolling(window - 1).sum()
        efficiency = 100 * change * change.abs() / distance.replace(0, np.nan)
        efficiency = efficiency.mask(distance.eq(0), 0)
        factors[code] = pd.DataFrame({
            "bias": bias_factor, "slope": slope_factor, "efficiency": efficiency,
        })
    scores = pd.DataFrame(0.0, index=next(iter(bars.values())).index,
                          columns=config["codes"])
    valid = pd.Series(True, index=scores.index)
    for name, weight in zip(("bias", "slope", "efficiency"), config["weights"]):
        cross = pd.DataFrame({code: frame[name] for code, frame in factors.items()})
        valid &= cross.notna().all(axis=1)
        std = cross.std(axis=1, ddof=0)
        z = cross.sub(cross.mean(axis=1), axis=0).div(std.replace(0, 1), axis=0)
        scores += weight * z
    return scores.where(valid, np.nan), factors


def choose_target(scores, holding, config):
    # 全池有效时选择最高分；负分采用绝对值差额阈值或原文直译阈值。
    if scores.isna().any():
        return holding
    best = scores.idxmax()
    if holding is None:
        return best
    if best == holding or scores[best] <= scores[holding]:
        return holding
    current = scores[holding]
    if config["threshold_mode"] == "literal":
        hurdle = current * config["threshold"]
    else:
        hurdle = current + (config["threshold"] - 1) * abs(current)
    return best if scores[best] > hurdle else holding


def load_bars(db_path, config):
    # 只读本地 ETF 日线并保留全池日期并集，缺失或无成交日不填充信号。
    codes = config["codes"]
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        placeholders = ",".join("?" for _ in codes)
        frame = pd.read_sql_query(
            f"SELECT * FROM etf_daily WHERE etf_code IN ({placeholders}) "
            "ORDER BY trade_date", conn, params=codes,
        )
    dates = pd.Index(sorted(frame["trade_date"].unique()), name="trade_date")
    bars = {}
    for code in codes:
        subset = frame.loc[frame.etf_code == code].set_index("trade_date").reindex(dates)
        valid = (subset[["open", "high", "low", "close"]].gt(0).all(axis=1)
                 & subset["volume"].gt(0) & subset["is_trading"].eq(1))
        subset.loc[~valid, ["open", "high", "low", "close"]] = np.nan
        bars[code] = subset
    return bars


def backtest(bars, scores, config):
    # 昨日收盘信号在今日开盘成交，单资产整手复利，停牌时保留持仓。
    cash = float(config["initial_cash"])
    holding, shares, pending, last_close = None, 0, None, 0.0
    fee = config["fee_bps"] / 10000
    slip = config["slippage_bps"] / 10000
    records, trades = [], []
    signal_date = None
    for day in scores.index:
        if day < config["start"]:
            continue
        if pending is not None and pending != holding:
            buy_open = bars[pending].at[day, "open"]
            sell_open = bars[holding].at[day, "open"] if holding else 1.0
            if pd.notna(buy_open) and pd.notna(sell_open):
                if holding:
                    price = sell_open * (1 - slip)
                    cash += shares * price * (1 - fee)
                    trades.append([day, signal_date, holding, "sell", shares, price])
                    holding, shares = None, 0
                price = buy_open * (1 + slip)
                lot = config["lot_size"]
                shares = int(cash / (price * (1 + fee)) // lot) * lot
                if shares:
                    cash -= shares * price * (1 + fee)
                    holding = pending
                    trades.append([day, signal_date, holding, "buy", shares, price])
        if holding:
            close = bars[holding].at[day, "close"]
            if pd.notna(close):
                last_close = close
        equity = cash + shares * last_close
        records.append([day, equity, cash, holding, shares])
        pending = choose_target(scores.loc[day], holding, config)
        signal_date = day
    curve = pd.DataFrame(records, columns=["date", "equity", "cash", "holding", "shares"])
    ledger = pd.DataFrame(trades, columns=["date", "signal_date", "code", "side",
                                         "shares", "price"])
    return curve, ledger, pending


def validate(config):
    # 校验窗口、权重、阈值及交易参数，拒绝无法解释的配置。
    if len(config["codes"]) < 2 or len(set(config["codes"])) != len(config["codes"]):
        raise ValueError("至少需要两个不重复的 ETF")
    for key in ("bias_window", "momentum_window", "slope_window", "efficiency_window"):
        if not isinstance(config[key], int) or config[key] < 2:
            raise ValueError(f"{key} 必须为不小于 2 的整数")
    weights = np.asarray(config["weights"], dtype=float)
    if weights.shape != (3,) or not np.isfinite(weights).all():
        raise ValueError("需要三个有限权重")
    if (weights < 0).any() or not np.isclose(weights.sum(), 1):
        raise ValueError("权重必须非负且合计为 1")
    if config["threshold_mode"] not in ("literal", "signed_gap"):
        raise ValueError("未知阈值模式")
    if not np.isfinite(config["threshold"]) or config["threshold"] < 1:
        raise ValueError("threshold 必须至少为 1")
    for key in ("fee_bps", "slippage_bps"):
        if not 0 <= config[key] < 10000:
            raise ValueError(f"{key} 超出范围")
    if config["initial_cash"] <= 0 or config["lot_size"] <= 0:
        raise ValueError("资金和交易单位必须为正")


def main():
    # 运行只读研究回测并导出因子、信号、交易、净值及参数快照。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/security_pool.db")
    parser.add_argument("--config", default="configs/etf_three_momentum.json")
    parser.add_argument("--output", default="data/etf_three_momentum")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    validate(config)
    bars = load_bars(args.db, config)
    scores, factors = make_scores(bars, config)
    curve, trades, pending = backtest(bars, scores, config)
    if curve.empty or not scores.notna().all(axis=1).any():
        raise ValueError("没有足够行情生成策略信号")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    scores.to_csv(output / "scores.csv", encoding="utf-8-sig")
    pd.concat(factors, names=["code"]).to_csv(output / "factors.csv", encoding="utf-8-sig")
    curve.to_csv(output / "equity.csv", index=False)
    trades.to_csv(output / "trades.csv", index=False)
    nav = curve.equity / config["initial_cash"]
    returns = curve.equity.pct_change().fillna(0)
    report = {
        "config": config, "price_basis": "raw_unadjusted_no_distributions",
        "start": curve.date.iloc[0], "end": curve.date.iloc[-1],
        "total_return": float(nav.iloc[-1] - 1),
        "annualized_return": float(nav.iloc[-1] ** (252 / len(nav)) - 1),
        "max_drawdown": float((nav / nav.cummax() - 1).min()),
        "sharpe_zero_rf": float(returns.mean() / returns.std() * np.sqrt(252)),
        "trade_count": len(trades), "next_target": pending,
        "latest_scores": scores.iloc[-1].to_dict(),
        "warning": "未复权价格研究结果，不含分红、拆合份及涨跌停成交限制，不可作实盘绩效",
    }
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
