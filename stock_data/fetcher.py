"""股票详细数据的通达信接口调用。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

from security_pool.fetcher import call_with_retry, init_tdx
from tdx_client import FIELD_MAP, TdxClient


LOGGER = logging.getLogger(__name__)

BAR_FIELDS = (
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "Amount",
    "ForwardFactor",
)

GP_FIELDS = tuple(
    sorted(
        (
            field
            for field in FIELD_MAP["get_gpjy_value"]
            if field.startswith("GP")
        ),
        key=lambda value: int(value[2:]),
    )
)

FN_FIELDS = tuple(
    sorted(
        (
            field
            for field in FIELD_MAP["get_financial_data"]
            if field.startswith("FN")
        ),
        key=lambda value: int(value[2:]),
    )
)

# 第一批核心财务字段控制在单次接口上限内，兼顾财务三表、质量、成长、
# 偿债、运营效率、TTM/单季度和审计风险。业绩预告与快报留到后续事件层。
CORE_FN_FIELDS = tuple(
    f"FN{number}"
    for number in (
        1, 2, 4, 6, 7,
        8, 11, 17, 21, 27, 35, 40, 41, 44, 52, 54, 55, 56, 63, 72,
        107, 114, 119, 124, 125, 128, 131, 133, 134,
        159, 160, 162, 172, 173, 175, 183, 184, 185, 187, 190, 191,
        194, 197, 199, 200, 202, 203, 206, 207, 208, 210, 219, 220,
        222, 223, 228, 229,
        230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 242,
        266, 271, 276, 281, 283, 304, 307, 308, 309, 311, 312, 319,
        321, 322, 323, 324, 327, 328, 329, 336, 337, 338, 339, 362,
    )
)


def to_tdx_date(value: str | date) -> str:
    # 将 ISO 日期转换为通达信 YYYYMMDD 日期。
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return date.fromisoformat(value).strftime("%Y%m%d")


def chunked(values: Sequence[str], size: int) -> list[list[str]]:
    # 将字段或股票列表拆成固定大小的批次。
    if size <= 0:
        raise ValueError("批次大小必须大于零")
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


def yearly_date_ranges(start_date: str, end_date: str) -> list[tuple[str, str]]:
    # 将日期范围拆成不重叠的自然年度窗口。
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    ranges: list[tuple[str, str]] = []
    current = start
    while current <= end:
        current_end = min(date(current.year, 12, 31), end)
        ranges.append((current.isoformat(), current_end.isoformat()))
        current = date(current.year + 1, 1, 1)
    return ranges


def fetch_daily_bars(
    client: TdxClient,
    stock_codes: Sequence[str],
    start_date: str,
    end_date: str,
) -> Any:
    # 获取指定股票和日期范围的不复权日线行情。
    return call_with_retry(
        client.get_market_data,
        list(BAR_FIELDS),
        list(stock_codes),
        "1d",
        to_tdx_date(start_date),
        to_tdx_date(end_date),
        -1,
        "none",
        False,
    )


def fetch_capital_history(
    client: TdxClient,
    stock_code: str,
    start_date: str,
    end_date: str,
) -> Any:
    # 获取单只股票指定日期范围内的每日股本。
    return call_with_retry(
        client.get_gb_info_by_date,
        stock_code,
        to_tdx_date(start_date),
        to_tdx_date(end_date),
    )


def fetch_corporate_actions(
    client: TdxClient,
    stock_code: str,
    start_date: str,
    end_date: str,
) -> Any:
    # 获取单只股票指定日期范围内的分红送配事件。
    return call_with_retry(
        client.get_divid_factors,
        stock_code,
        to_tdx_date(start_date),
        to_tdx_date(end_date),
    )


def fetch_trade_metrics(
    client: TdxClient,
    stock_codes: Sequence[str],
    start_date: str,
    end_date: str,
    fields: Sequence[str] = GP_FIELDS,
    field_batch_size: int = 13,
    split_by_year: bool = False,
) -> list[Any]:
    # 按字段批次获取 GP 指标，并可选按自然年拆分修复窗口。
    results: list[Any] = []
    date_ranges = (
        yearly_date_ranges(start_date, end_date)
        if split_by_year
        else [(start_date, end_date)]
    )
    for range_start, range_end in date_ranges:
        for field_batch in chunked(tuple(fields), field_batch_size):
            result = call_with_retry(
                client.get_gpjy_value,
                list(stock_codes),
                field_batch,
                to_tdx_date(range_start),
                to_tdx_date(range_end),
            )
            results.append(result)
    return results


def fetch_financial_reports(
    client: TdxClient,
    stock_codes: Sequence[str],
    start_date: str,
    end_date: str,
    fields: Sequence[str] = FN_FIELDS,
    field_batch_size: int = 100,
) -> list[Any]:
    # 分字段批次获取按公告日筛选的专业财务数据。
    results: list[Any] = []
    for field_batch in chunked(tuple(fields), field_batch_size):
        result = call_with_retry(
            client.get_financial_data,
            list(stock_codes),
            field_batch,
            to_tdx_date(start_date),
            to_tdx_date(end_date),
            "announce_time",
        )
        results.append(result)
    return results


__all__ = [
    "BAR_FIELDS",
    "CORE_FN_FIELDS",
    "FN_FIELDS",
    "GP_FIELDS",
    "chunked",
    "fetch_capital_history",
    "fetch_corporate_actions",
    "fetch_daily_bars",
    "fetch_financial_reports",
    "fetch_trade_metrics",
    "init_tdx",
    "to_tdx_date",
    "yearly_date_ranges",
]
