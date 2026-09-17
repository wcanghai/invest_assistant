"""股票详细数据的扁平化、字段映射和校验。"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from invest.market.security_pool.processor import normalize_code
from invest.market.security_pool.processor import now_text
from invest.market.security_pool.processor import parse_date
from invest.market.security_pool.processor import parse_float
from invest.market.security_pool.processor import parse_int

from invest.providers.stock import BAR_FIELDS


BAR_COLUMN_MAP = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "Amount": "amount_10k",
    "ForwardFactor": "forward_factor",
}

TRADE_FIELD_MAP = {
    "shareholder_count": ("GP01", 0),
    "financing_balance_10k": ("GP03", 0),
    "securities_lending_balance_shares": ("GP03", 1),
    "northbound_holding_shares": ("GP06", 0),
    "financing_buy_10k": ("GP11", 0),
    "financing_repay_10k": ("GP11", 1),
    "financing_net_buy_10k": ("GP13", 0),
    "total_market_cap_10k": ("GP16", 0),
    "dividend_yield_pct": ("GP21", 0),
    "limit_status": ("GP15", 0),
    "limit_order_amount_10k": ("GP15", 1),
    "pledge_ratio_pct": ("GP20", 0),
    "market_popularity_rank": ("GP27", 0),
    "industry_popularity_rank": ("GP27", 1),
}

FINANCIAL_FIELD_MAP = {
    "basic_eps": ("FN1",),
    "deduct_eps": ("FN2",),
    "book_value_per_share": ("FN4",),
    "roe_pct": ("FN6", "FN197"),
    "total_assets": ("FN40",),
    "total_liabilities": ("FN63",),
    "total_equity": ("FN72",),
    "operating_cash_flow": ("FN234", "FN107"),
    "net_profit": ("FN134",),
    "revenue": ("FN230",),
    "operating_profit": ("FN231",),
    "parent_net_profit": ("FN232",),
    "deduct_net_profit": ("FN233", "FN206"),
    "revenue_growth_pct": ("FN183",),
    "net_profit_growth_pct": ("FN184",),
    "gross_margin_pct": ("FN202",),
    "debt_ratio_pct": ("FN210",),
    "total_shares": ("FN238",),
    "float_a_shares": ("FN239",),
    "shareholder_count": ("FN242",),
}


def _is_missing(value: Any) -> bool:
    # 判断接口值是否为空或非有限浮点数。
    if value is None:
        return True
    if isinstance(value, float):
        return not math.isfinite(value)
    return False


def _json_value(value: Any) -> Any:
    # 将 numpy 等接口标量转换为可序列化的基础类型。
    if _is_missing(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _iter_frame_rows(frame: Any) -> list[tuple[Any, Any]]:
    # 返回类似 DataFrame 对象的索引和行序列。
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    return list(frame.iterrows())


def flatten_daily_bars(raw_data: Any) -> list[dict[str, Any]]:
    # 将字段到 DataFrame 的行情字典转换为股票日线记录。
    if not isinstance(raw_data, Mapping):
        return []
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for source_field in BAR_FIELDS:
        frame = raw_data.get(source_field)
        target_field = BAR_COLUMN_MAP[source_field]
        for index, series in _iter_frame_rows(frame):
            trade_date = parse_date(index)
            if trade_date is None or not hasattr(series, "items"):
                continue
            for raw_code, value in series.items():
                stock_code = normalize_code(raw_code)
                if stock_code is None:
                    continue
                key = (stock_code, trade_date)
                row = records.setdefault(
                    key,
                    {"stock_code": stock_code, "trade_date": trade_date},
                )
                row[target_field] = parse_float(value)

    timestamp = now_text()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records.values():
        grouped[row["stock_code"]].append(row)
    output: list[dict[str, Any]] = []
    for stock_code in sorted(grouped):
        previous_close: float | None = None
        for row in sorted(grouped[stock_code], key=lambda item: item["trade_date"]):
            close = row.get("close")
            row["pre_close"] = previous_close
            row["pct_change"] = None
            if close is not None and previous_close not in {None, 0}:
                row["pct_change"] = (close / previous_close - 1.0) * 100.0
            row["is_trading"] = int((row.get("volume") or 0) > 0)
            row["updated_at"] = timestamp
            output.append(row)
            if close is not None:
                previous_close = close
    return output


def build_capital_rows(stock_code: str, raw_rows: Any) -> list[dict[str, Any]]:
    # 将每日股本接口记录转换为数据库行。
    code = normalize_code(stock_code)
    if code is None or not isinstance(raw_rows, (list, tuple)):
        return []
    timestamp = now_text()
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        trade_date = parse_date(raw_row.get("Date"))
        if trade_date is None:
            continue
        rows.append({
            "stock_code": code,
            "trade_date": trade_date,
            "total_shares": parse_float(raw_row.get("Zgb")),
            "float_shares": parse_float(raw_row.get("Ltgb")),
            "updated_at": timestamp,
        })
    return rows


def build_action_rows(stock_code: str, raw_frame: Any) -> list[dict[str, Any]]:
    # 将分红送配 DataFrame 转换为公司行为记录。
    code = normalize_code(stock_code)
    if code is None:
        return []
    timestamp = now_text()
    rows: list[dict[str, Any]] = []
    for index, raw_row in _iter_frame_rows(raw_frame):
        event_date = parse_date(index)
        action_type = parse_int(raw_row.get("Type"))
        if event_date is None or action_type is None:
            continue
        rows.append({
            "stock_code": code,
            "event_date": event_date,
            "action_type": action_type,
            "cash_bonus": parse_float(raw_row.get("Bonus")),
            "allot_price": parse_float(
                raw_row.get("AllotPrice", raw_row.get("AlloPrice"))
            ),
            "share_bonus": parse_float(raw_row.get("ShareBonus")),
            "allotment": parse_float(raw_row.get("Allotment")),
            "updated_at": timestamp,
        })
    return rows


def _metric_value(metrics: Mapping[str, Any], field: str, index: int) -> float | None:
    # 提取一个 GP 指标指定位置的数值。
    values = metrics.get(field)
    if not isinstance(values, (list, tuple)) or index >= len(values):
        return None
    return parse_float(values[index])


def pivot_trade_metrics(raw_parts: Sequence[Any]) -> list[dict[str, Any]]:
    # 将多个 GP 接口分片透视为股票每日宽记录。
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_data in raw_parts:
        if not isinstance(raw_data, Mapping):
            continue
        for raw_code, stock_data in raw_data.items():
            stock_code = normalize_code(raw_code)
            if stock_code is None or not isinstance(stock_data, Mapping):
                continue
            for field, metric_rows in stock_data.items():
                if not isinstance(metric_rows, (list, tuple)):
                    continue
                for metric_row in metric_rows:
                    if not isinstance(metric_row, Mapping):
                        continue
                    trade_date = parse_date(metric_row.get("Date"))
                    if trade_date is None:
                        continue
                    key = (stock_code, trade_date)
                    record = records.setdefault(
                        key,
                        {
                            "stock_code": stock_code,
                            "trade_date": trade_date,
                            "_metrics": {},
                        },
                    )
                    values = metric_row.get("Value")
                    if isinstance(values, (list, tuple)):
                        record["_metrics"][field] = [
                            _json_value(value) for value in values
                        ]

    timestamp = now_text()
    output: list[dict[str, Any]] = []
    for key in sorted(records):
        record = records[key]
        metrics = record.pop("_metrics")
        for target, (source, index) in TRADE_FIELD_MAP.items():
            value = _metric_value(metrics, source, index)
            record[target] = parse_int(value) if target == "limit_status" else value
        record["raw_metrics_json"] = json.dumps(
            metrics,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        record["updated_at"] = timestamp
        output.append(record)
    return output


def _financial_value(values: Mapping[str, Any], fields: Sequence[str]) -> float | None:
    # 从多个候选 FN 字段中提取第一个有效数值。
    for field in fields:
        value = parse_float(values.get(field))
        if value is not None:
            return value
    return None


def build_financial_rows(raw_parts: Sequence[Any]) -> list[dict[str, Any]]:
    # 合并财务字段分片并生成每份报告一行的数据。
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw_data in raw_parts:
        if not isinstance(raw_data, Mapping):
            continue
        for raw_code, frame in raw_data.items():
            stock_code = normalize_code(raw_code)
            if stock_code is None:
                continue
            for _, raw_row in _iter_frame_rows(frame):
                report_date = parse_date(raw_row.get("tag_time"))
                announce_date = parse_date(raw_row.get("announce_time"))
                if report_date is None or announce_date is None:
                    continue
                key = (stock_code, report_date, announce_date)
                record = records.setdefault(
                    key,
                    {
                        "stock_code": stock_code,
                        "report_date": report_date,
                        "announce_date": announce_date,
                        "_values": {},
                    },
                )
                for field, value in raw_row.items():
                    if not str(field).startswith("FN") or _is_missing(value):
                        continue
                    record["_values"][str(field)] = _json_value(value)

    timestamp = now_text()
    output: list[dict[str, Any]] = []
    for key in sorted(records):
        record = records[key]
        values = record.pop("_values")
        for target, sources in FINANCIAL_FIELD_MAP.items():
            record[target] = _financial_value(values, sources)
        record["raw_values_json"] = json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        record["updated_at"] = timestamp
        output.append(record)
    return output


def validate_bar_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    # 检查日线主键、价格关系和非负成交量金额。
    errors: list[str] = []
    seen: set[tuple[Any, Any]] = set()
    for row in rows:
        key = (row.get("stock_code"), row.get("trade_date"))
        if key in seen:
            errors.append(f"日线主键重复: {key}")
        seen.add(key)
        low = row.get("low")
        high = row.get("high")
        open_price = row.get("open")
        close = row.get("close")
        if low is not None and high is not None and low > high:
            errors.append(f"日线高低价异常: {key}")
        if low is not None and high is not None:
            if open_price is not None and not low <= open_price <= high:
                errors.append(f"日线开盘价越界: {key}")
            if close is not None and not low <= close <= high:
                errors.append(f"日线收盘价越界: {key}")
        if (row.get("volume") or 0) < 0 or (row.get("amount_10k") or 0) < 0:
            errors.append(f"日线成交量额为负: {key}")
    return errors


def validate_capital_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    # 检查股本为非负且总股本不小于流通股本。
    errors: list[str] = []
    for row in rows:
        total = row.get("total_shares")
        floating = row.get("float_shares")
        key = (row.get("stock_code"), row.get("trade_date"))
        if (total is not None and total < 0) or (floating is not None and floating < 0):
            errors.append(f"股本为负: {key}")
        if total is not None and floating is not None and floating > total * 1.000001:
            errors.append(f"流通股本大于总股本: {key}")
    return errors


def validate_financial_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    # 检查财务报告主键和原始 JSON 的完整性。
    errors: list[str] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for row in rows:
        key = (
            row.get("stock_code"),
            row.get("report_date"),
            row.get("announce_date"),
        )
        if key in seen:
            errors.append(f"财务主键重复: {key}")
        seen.add(key)
        try:
            json.loads(str(row.get("raw_values_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append(f"财务 JSON 无效: {key}")
    return errors
