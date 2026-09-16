"""通达信接口调用、重试和基础返回格式处理。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from tdx_client import TdxClient

from .processor import clean_name, normalize_code


LOGGER = logging.getLogger(__name__)
T = TypeVar("T")

CLASSIFICATIONS = {
    "5": "所有A股",
    "7": "上证主板",
    "8": "深证主板",
    "23": "沪深300",
    "24": "中证500",
    "25": "中证1000",
    "28": "中证A500",
    "50": "沪深A股",
    "51": "创业板",
    "52": "科创板",
    "53": "北交所",
    "56": "沪深股通",
    "57": "融资融券",
}

MORE_INFO_FIELDS = (
    "HqDate",
    "TPFlag",
    "Zsz",
    "Ltsz",
    "fHSL",
    "CJJEPre1",
    "FreeLtgb",
    "StaticPE_TTM",
    "PB_MRQ",
    "DYRatio",
)


def init_tdx(api: Any = None, script_file: str | None = None) -> TdxClient:
    # 创建并初始化通达信客户端。
    client = TdxClient(script_file=script_file or __file__, api=api)
    client.initialize()
    return client


def _check_api_error(result: Any) -> None:
    # 检查通达信字典结果中的错误码。
    if not isinstance(result, dict):
        return
    error_id = result.get("ErrorId")
    if error_id is not None and str(error_id) not in {"", "0"}:
        message = result.get("Msg") or result.get("ErrorMsg") or "未知接口错误"
        raise RuntimeError(f"ErrorId={error_id}: {message}")


def call_with_retry(
    function: Callable[..., T],
    *args: Any,
    retries: int = 3,
    retry_delay: float = 0.5,
    **kwargs: Any,
) -> T:
    # 调用接口并对临时异常进行有限次数重试。
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            result = function(*args, **kwargs)
            _check_api_error(result)
            return result
        except Exception as exc:
            last_error = exc
            LOGGER.warning(
                "接口 %s 第 %s/%s 次调用失败: %s",
                getattr(function, "__name__", repr(function)),
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                time.sleep(retry_delay * attempt)
    assert last_error is not None
    raise last_error


def _as_records(result: Any) -> list[dict[str, Any]]:
    # 将接口列表结果转换为字典记录列表。
    if result is None:
        return []
    if isinstance(result, list):
        return [dict(item) for item in result if isinstance(item, dict)]
    if isinstance(result, tuple):
        return [dict(item) for item in result if isinstance(item, dict)]
    return []


def _stock_members(result: Any) -> dict[str, str]:
    # 将证券列表接口结果转换为代码到名称的映射。
    members: dict[str, str] = {}
    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, dict):
                code = normalize_code(item.get("Code"))
                name = clean_name(item.get("Name")) or ""
            else:
                code = normalize_code(item)
                name = ""
            if code:
                members[code] = name
    return members


def fetch_classifications(
    client: TdxClient,
) -> dict[str, dict[str, str] | None]:
    # 获取全部核心系统分类，核心 A 股分类失败时直接终止。
    snapshots: dict[str, dict[str, str] | None] = {}
    for market_code, market_name in CLASSIFICATIONS.items():
        try:
            result = call_with_retry(client.get_stock_list, market_code, 1)
            members = _stock_members(result)
            if market_code == "5" and not members:
                raise RuntimeError("所有 A 股分类返回为空")
            snapshots[market_code] = members
            LOGGER.info("分类 %s(%s) 获取 %s 条", market_name, market_code, len(members))
        except Exception:
            if market_code == "5":
                raise
            LOGGER.exception("非核心分类 %s(%s) 获取失败", market_name, market_code)
            snapshots[market_code] = None
    return snapshots


def fetch_stock_info(client: TdxClient, stock_code: str) -> dict[str, Any] | None:
    # 获取单只股票的证券基本信息。
    try:
        result = call_with_retry(client.get_stock_info, stock_code, [])
        if not isinstance(result, dict) or not result.get("Name"):
            return None
        return dict(result)
    except Exception:
        LOGGER.exception("股票 %s 基本信息获取失败", stock_code)
        return None


def fetch_stock_daily_info(
    client: TdxClient,
    stock_code: str,
    fields: Sequence[str] = MORE_INFO_FIELDS,
) -> dict[str, Any] | None:
    # 获取单只股票的证券池每日动态字段。
    try:
        result = call_with_retry(client.get_more_info, stock_code, list(fields))
        if not isinstance(result, dict) or not result.get("HqDate"):
            return None
        return dict(result)
    except Exception:
        LOGGER.exception("股票 %s 动态信息获取失败", stock_code)
        return None


def fetch_stock_relations(
    client: TdxClient,
    stock_code: str,
) -> list[dict[str, Any]] | None:
    # 获取单只股票当前所属板块关系。
    try:
        return _as_records(call_with_retry(client.get_relation, stock_code))
    except Exception:
        LOGGER.exception("股票 %s 板块关系获取失败", stock_code)
        return None


def fetch_stock_bundle(
    client: TdxClient,
    stock_code: str,
    include_relations: bool = True,
) -> dict[str, Any]:
    # 获取构建单只股票证券池记录所需的全部接口数据。
    relations = fetch_stock_relations(client, stock_code) if include_relations else None
    return {
        "stock_info": fetch_stock_info(client, stock_code),
        "daily_info": fetch_stock_daily_info(client, stock_code),
        "relations": relations,
    }


def fetch_ipo_info(client: TdxClient) -> list[dict[str, Any]] | None:
    # 分别获取新股和新债申购信息并标记发行类型。
    records: list[dict[str, Any]] = []
    try:
        for ipo_type, issue_type in ((0, "STOCK"), (1, "BOND")):
            result = call_with_retry(client.get_ipo_info, ipo_type, 1)
            for item in _as_records(result):
                item["_issue_type"] = issue_type
                records.append(item)
        return records
    except Exception:
        LOGGER.exception("新股新债申购信息获取失败")
        return None


def fetch_convertible_bonds(client: TdxClient) -> list[dict[str, Any]] | None:
    # 获取全部当前可转债的详细信息。
    try:
        bond_members = _stock_members(call_with_retry(client.get_stock_list, "32", 1))
    except Exception:
        LOGGER.exception("可转债列表获取失败")
        return None
    records: list[dict[str, Any]] = []
    for bond_code in sorted(bond_members):
        try:
            result = call_with_retry(client.get_kzz_info, bond_code, [])
            if isinstance(result, dict):
                record = dict(result)
                record["_stock_code"] = bond_code
                records.append(record)
        except Exception:
            LOGGER.exception("可转债 %s 详细信息获取失败", bond_code)
    return records


def fetch_etfs(client: TdxClient) -> list[dict[str, Any]] | None:
    # 获取全部 ETF 跟踪指数及对应 ETF 当前数据。
    try:
        index_members = _stock_members(call_with_retry(client.get_stock_list, "91", 1))
    except Exception:
        LOGGER.exception("ETF 跟踪指数列表获取失败")
        return None
    records: list[dict[str, Any]] = []
    for index_code in sorted(index_members):
        if index_code.upper().endswith(".OT"):
            LOGGER.warning("跳过接口不支持的 OT 指数 %s", index_code)
            continue
        try:
            result = call_with_retry(client.get_trackzs_etf_info, index_code)
            for item in _as_records(result):
                item["_index_code"] = index_code
                records.append(item)
        except Exception:
            LOGGER.exception("指数 %s 的 ETF 信息获取失败", index_code)
    return records
