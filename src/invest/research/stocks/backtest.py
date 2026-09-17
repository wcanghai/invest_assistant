"""简化的月度多因子组合回测。"""

from __future__ import annotations

import uuid
from typing import Any

import numpy as np
import pandas as pd

from invest.storage import factor as db
from invest.research.stocks.data import load_factor_data
from invest.research.stocks.portfolio import build_portfolio
from invest.research.stocks.scoring import calculate_scores


def _month_end_dates(trade_dates: list[str]) -> list[str]:
    # 从有序交易日中选择每个月的最后一天。
    frame = pd.DataFrame({"trade_date": pd.to_datetime(trade_dates)})
    if frame.empty:
        return []
    selected = frame.groupby(frame["trade_date"].dt.to_period("M")).tail(1)
    return selected["trade_date"].dt.strftime("%Y-%m-%d").tolist()


def _fee(value: float, side: str, config: dict[str, Any]) -> float:
    # 根据买卖方向计算佣金、税费和固定冲击成本。
    settings = config["execution"]
    rate = float(settings[f"{side}_fee_bps"]) + float(settings["impact_bps"])
    if side == "sell":
        rate += float(settings["sell_tax_bps"])
    return value * rate / 10_000.0


def _blocked(side: str, row: pd.Series) -> str | None:
    # 根据停牌、成交量和涨跌停状态返回拒绝原因。
    if int(row.get("is_trading", 0)) != 1 or float(row.get("volume", 0)) <= 0:
        return "SUSPENDED"
    status = row.get("limit_status")
    if pd.notna(status) and side == "buy" and float(status) > 0:
        return "LIMIT_UP"
    if pd.notna(status) and side == "sell" and float(status) < 0:
        return "LIMIT_DOWN"
    return None


def _execute_target(
    target: pd.DataFrame,
    market: pd.DataFrame,
    holdings: dict[str, int],
    cash: float,
    nav: float,
    signal_date: str,
    trade_date: str,
    config: dict[str, Any],
) -> tuple[dict[str, int], float, list[dict[str, Any]]]:
    # 按次日开盘、整手、容量和交易状态执行目标组合。
    prices = market.set_index("stock_code")
    lot = int(config["execution"]["lot_size"])
    participation = float(config["portfolio"]["max_adv_participation"])
    target_weights = target.set_index("stock_code")["target_weight"].to_dict()
    all_codes = set(holdings) | set(target_weights)
    orders: list[tuple[str, str, int]] = []
    for code in all_codes:
        if code not in prices.index:
            continue
        open_price = prices.at[code, "open"]
        if pd.isna(open_price) or float(open_price) <= 0:
            continue
        price = float(open_price)
        target_quantity = int(nav * target_weights.get(code, 0.0) / price / lot) * lot
        difference = target_quantity - holdings.get(code, 0)
        if difference != 0:
            orders.append((code, "buy" if difference > 0 else "sell", abs(difference)))
    orders.sort(key=lambda item: item[1], reverse=True)
    trades: list[dict[str, Any]] = []
    for code, side, quantity in orders:
        row = prices.loc[code]
        reason = _blocked(side, row)
        price = float(row["open"])
        amount_capacity = float(row.get("amount_10k", 0)) * 10_000 * participation
        capacity_quantity = int(amount_capacity / price / lot) * lot
        filled = min(quantity, max(0, capacity_quantity))
        if side == "sell":
            filled = min(filled, holdings.get(code, 0))
        if reason is not None:
            filled = 0
        if side == "buy" and filled > 0:
            maximum_affordable = int(cash / price / lot) * lot
            filled = min(filled, maximum_affordable)
        value = filled * price
        fee = _fee(value, side, config) if filled > 0 else 0.0
        if side == "buy" and filled > 0:
            while filled > 0 and value + fee > cash:
                filled -= lot
                value = filled * price
                fee = _fee(value, side, config)
            holdings[code] = holdings.get(code, 0) + filled
            cash -= value + fee
        elif side == "sell" and filled > 0:
            holdings[code] -= filled
            cash += value - fee
            if holdings[code] == 0:
                holdings.pop(code)
        status = "FILLED" if filled == quantity else "PARTIAL"
        if filled == 0:
            status = "REJECTED"
            reason = reason or "CAPACITY_OR_CASH"
        trades.append({
            "order_id": uuid.uuid4().hex,
            "signal_date": signal_date,
            "trade_date": trade_date,
            "stock_code": code,
            "side": side.upper(),
            "target_quantity": quantity,
            "filled_quantity": filled,
            "trade_price": price if filled > 0 else None,
            "fee": fee,
            "status": status,
            "reason": reason,
        })
    return holdings, cash, trades


def run_backtest(
    connection: Any,
    start_date: str,
    end_date: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    # 按月生成目标组合并逐日计算现金、持仓和净值。
    trade_dates = db.load_trade_dates(connection, end_date)
    trade_dates = [value for value in trade_dates if value >= start_date]
    factor_dates = _month_end_dates(trade_dates)
    targets: dict[str, tuple[str, pd.DataFrame]] = {}
    previous_target: pd.DataFrame | None = None
    for factor_date in factor_dates:
        execution_date = db.next_trade_date(connection, factor_date)
        if execution_date is None or execution_date > end_date:
            continue
        data = load_factor_data(connection, factor_date, config)
        scores = calculate_scores(data, config)
        target = build_portfolio(scores, previous_target, config)
        targets[execution_date] = (factor_date, target)
        previous_target = target[["stock_code", "target_weight"]].copy()
    initial_cash = float(config["execution"]["initial_cash"])
    cash = initial_cash
    holdings: dict[str, int] = {}
    last_prices: dict[str, float] = {}
    active_target: tuple[str, pd.DataFrame] | None = None
    attempts_remaining = 0
    peak = initial_cash
    previous_nav = initial_cash
    nav_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    backtest_id = uuid.uuid4().hex
    for trade_date in trade_dates:
        market = db.load_market_day(connection, trade_date)
        close_map = market.set_index("stock_code")["close"].dropna().to_dict()
        last_prices.update({code: float(value) for code, value in close_map.items()})
        holding_value = sum(
            quantity * last_prices.get(code, 0.0)
            for code, quantity in holdings.items()
        )
        nav = cash + holding_value
        if trade_date in targets:
            active_target = targets[trade_date]
            attempts_remaining = int(config["execution"]["pending_days"])
        if active_target is not None and attempts_remaining > 0:
            signal_date, target = active_target
            holdings, cash, trades = _execute_target(
                target,
                market,
                holdings,
                cash,
                nav,
                signal_date,
                trade_date,
                config,
            )
            for trade in trades:
                trade["backtest_id"] = backtest_id
            trade_rows.extend(trades)
            holding_value = sum(
                quantity * last_prices.get(code, 0.0)
                for code, quantity in holdings.items()
            )
            nav = cash + holding_value
            attempts_remaining -= 1
            if attempts_remaining == 0:
                active_target = None
        peak = max(peak, nav)
        nav_rows.append({
            "backtest_id": backtest_id,
            "trade_date": trade_date,
            "nav": nav,
            "cash": cash,
            "holding_value": holding_value,
            "daily_return": nav / previous_nav - 1.0 if previous_nav > 0 else np.nan,
            "benchmark_return": None,
            "drawdown": nav / peak - 1.0,
        })
        previous_nav = nav
    return {
        "backtest_id": backtest_id,
        "nav": pd.DataFrame(nav_rows),
        "trades": pd.DataFrame(trade_rows),
    }
