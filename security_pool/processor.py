"""证券池字段标准化、宽表构建和数据校验。"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any, Mapping


CLASSIFICATION_FIELDS = {
    "5": "is_all_a",
    "7": "is_main_board",
    "8": "is_main_board",
    "23": "is_hs300",
    "24": "is_zz500",
    "25": "is_zz1000",
    "28": "is_a500",
    "51": "is_gem",
    "52": "is_star",
    "53": "is_bj_a",
    "56": "is_stock_connect",
    "57": "is_marginable",
}

POOL_FLAG_FIELDS = tuple(dict.fromkeys(CLASSIFICATION_FIELDS.values()))


def now_text() -> str:
    # 返回适合 SQLite 保存的当前本地时间。
    return datetime.now().isoformat(timespec="seconds")


def normalize_code(value: Any) -> str | None:
    # 标准化带市场后缀的证券代码。
    if value is None:
        return None
    code = str(value).strip().upper()
    if not code or "." not in code:
        return None
    symbol, market = code.rsplit(".", 1)
    if not symbol or not market:
        return None
    return f"{symbol}.{market}"


def normalize_optional_code(value: Any) -> str | None:
    # 标准化可能不带市场后缀的关联证券代码。
    if value is None:
        return None
    code = str(value).strip().upper()
    return code or None


def parse_date(value: Any) -> str | None:
    # 将通达信日期转换为 YYYY-MM-DD 文本。
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if text in {"", "0", "0.0", "None"}:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    # 将接口数值转换为有限浮点数。
    if value is None or value == "":
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_int(value: Any) -> int | None:
    # 将接口数值转换为整数。
    number = parse_float(value)
    return int(number) if number is not None else None


def parse_flag(value: Any) -> int | None:
    # 将接口标识统一为 0、1 或空值。
    if isinstance(value, bool):
        return int(value)
    number = parse_int(value)
    if number in {0, 1}:
        return number
    return None


def exchange_from_code(stock_code: str) -> str:
    # 从标准证券代码提取交易所后缀。
    return stock_code.rsplit(".", 1)[-1]


def clean_name(value: Any) -> str | None:
    # 清理证券和板块名称两端空格。
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _classification_flag(
    stock_code: str,
    field_name: str,
    classifications: Mapping[str, Mapping[str, str] | None],
    previous_row: Mapping[str, Any] | None,
) -> int | None:
    # 合并可能来自多个分类代码的同一证券池标识。
    matching_codes = [
        code for code, target_field in CLASSIFICATION_FIELDS.items()
        if target_field == field_name
    ]
    available_sets = [classifications.get(code) for code in matching_codes]
    known_sets = [members for members in available_sets if members is not None]
    if known_sets:
        return int(any(stock_code in members for members in known_sets))
    if previous_row is not None:
        return previous_row.get(field_name)
    return None


def build_master_row(
    stock_code: str,
    stock_info: Mapping[str, Any] | None,
    data_date: str,
) -> dict[str, Any]:
    # 从证券基本信息生成股票主表记录。
    info = stock_info or {}
    timestamp = now_text()
    return {
        "stock_code": stock_code,
        "exchange": exchange_from_code(stock_code),
        "security_kind": parse_int(info.get("HSStockKind")),
        "list_date": parse_date(info.get("J_start")),
        "initial_name": clean_name(info.get("Name")),
        "trade_unit": parse_float(info.get("Unit")),
        "min_price_tick": parse_float(info.get("MinPrice")),
        "price_precision": parse_int(info.get("XsFlag")),
        "first_seen_date": data_date,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def calculate_tradable(
    row: Mapping[str, Any],
    information_complete: bool,
) -> tuple[int, list[str]]:
    # 根据公共证券状态计算基础可交易标识和排除原因。
    reasons: list[str] = []
    if row.get("is_active") != 1:
        reasons.append("NOT_IN_ACTIVE_UNIVERSE")
    if row.get("is_st") == 1:
        reasons.append("ST")
    if row.get("is_delisting_board") == 1:
        reasons.append("DELISTING_BOARD")
    if row.get("is_suspended") == 1:
        reasons.append("SUSPENDED")
    if not information_complete:
        reasons.append("INCOMPLETE_INFO")
    return (0 if reasons else 1), reasons


def build_current_row(
    stock_code: str,
    data_date: str,
    classifications: Mapping[str, Mapping[str, str] | None],
    stock_info: Mapping[str, Any] | None,
    daily_info: Mapping[str, Any] | None,
    previous_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # 合并分类、基本信息和动态信息生成当前证券池宽表记录。
    info = stock_info or {}
    daily = daily_info or {}
    previous = dict(previous_row or {})
    all_a_members = classifications.get("5") or {}
    current_name = clean_name(info.get("Name"))
    if current_name is None:
        current_name = clean_name(all_a_members.get(stock_code))
    if current_name is None:
        current_name = previous.get("stock_name")

    row: dict[str, Any] = {
        "stock_code": stock_code,
        "data_date": data_date,
        "stock_name": current_name,
        "is_active": int(stock_code in all_a_members),
        "is_st": parse_flag(info.get("IsSTGP")),
        "is_delisting_board": parse_flag(info.get("IsQuitGP")),
        "is_suspended": parse_flag(daily.get("TPFlag")),
        "has_convertible_bond": parse_flag(info.get("BelongHasKQZ")),
        "industry_code": clean_name(info.get("rs_hycode_sim")),
        "industry_name": clean_name(info.get("rs_hyname")),
        "region_code": clean_name(info.get("tdx_dycode")),
        "region_name": clean_name(info.get("tdx_dyname")),
        "total_shares_10k": parse_float(info.get("J_zgb")),
        "float_shares_10k": parse_float(info.get("ActiveCapital")),
        "free_float_shares_10k": parse_float(daily.get("FreeLtgb")),
        "total_market_cap_100m": parse_float(daily.get("Zsz")),
        "float_market_cap_100m": parse_float(daily.get("Ltsz")),
        "turnover_rate": parse_float(daily.get("fHSL")),
        "amount_prev_1d_10k": parse_float(daily.get("CJJEPre1")),
        "pe_ttm": parse_float(daily.get("StaticPE_TTM")),
        "pb_mrq": parse_float(daily.get("PB_MRQ")),
        "dividend_yield": parse_float(daily.get("DYRatio")),
        "updated_at": now_text(),
    }
    for field_name in POOL_FLAG_FIELDS:
        row[field_name] = _classification_flag(
            stock_code,
            field_name,
            classifications,
            previous_row,
        )

    stable_daily_fields = (
        "is_st",
        "is_delisting_board",
        "has_convertible_bond",
        "industry_code",
        "industry_name",
        "region_code",
        "region_name",
        "total_shares_10k",
        "float_shares_10k",
    )
    if stock_info is None:
        for field_name in stable_daily_fields:
            row[field_name] = previous.get(field_name)

    information_complete = stock_info is not None and daily_info is not None
    row["is_tradable"], reasons = calculate_tradable(row, information_complete)
    row["exclusion_reasons"] = json.dumps(reasons, ensure_ascii=False)
    return row


def build_inactive_row(
    previous_row: Mapping[str, Any],
    data_date: str,
) -> dict[str, Any]:
    # 从上一日记录生成已退出股票的当日非活动记录。
    row = dict(previous_row)
    row["data_date"] = data_date
    row["is_active"] = 0
    row["is_all_a"] = 0
    for field_name in (
        "is_suspended",
        "free_float_shares_10k",
        "total_market_cap_100m",
        "float_market_cap_100m",
        "turnover_rate",
        "amount_prev_1d_10k",
        "pe_ttm",
        "pb_mrq",
        "dividend_yield",
    ):
        row[field_name] = None
    row["is_tradable"] = 0
    row["exclusion_reasons"] = json.dumps(
        ["NOT_IN_ACTIVE_UNIVERSE"],
        ensure_ascii=False,
    )
    row["updated_at"] = now_text()
    return row


def build_sector_rows(
    stock_code: str,
    relations: list[Mapping[str, Any]] | None,
    data_date: str,
    source: str = "RELATION",
) -> list[dict[str, Any]]:
    # 将股票板块关系转换为每日快照记录。
    rows: list[dict[str, Any]] = []
    for relation in relations or []:
        sector_name = clean_name(relation.get("BlockName"))
        if not sector_name:
            continue
        rows.append({
            "snapshot_date": data_date,
            "stock_code": stock_code,
            "sector_code": clean_name(relation.get("BlockCode")) or "",
            "sector_name": sector_name,
            "sector_type": clean_name(relation.get("BlockType")) or "",
            "source": source,
            "archived_at": now_text(),
        })
    return rows


def build_ipo_rows(
    records: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    # 将新股新债申购记录转换为数据库行。
    rows: list[dict[str, Any]] = []
    for record in records or []:
        code = normalize_optional_code(record.get("Code"))
        subscription_date = parse_date(record.get("SGDate"))
        if not code or not subscription_date:
            continue
        rows.append({
            "security_code": code,
            "security_name": clean_name(record.get("Name")),
            "issue_type": record.get("_issue_type", "UNKNOWN"),
            "subscription_date": subscription_date,
            "subscription_price": parse_float(record.get("SGPrice")),
            "subscription_code": normalize_optional_code(record.get("SGCode")),
            "max_subscription": parse_float(record.get("MaxSG")),
            "issue_pe": parse_float(record.get("PE_Issue")),
            "updated_at": now_text(),
        })
    return rows


def build_convertible_bond_rows(
    records: list[Mapping[str, Any]] | None,
    data_date: str,
) -> list[dict[str, Any]]:
    # 将可转债接口结果转换为当前表记录。
    rows: list[dict[str, Any]] = []
    for record in records or []:
        code = normalize_optional_code(record.get("_stock_code") or record.get("KZZCode"))
        if not code:
            continue
        rows.append({
            "bond_code": code,
            "underlying_code": normalize_optional_code(record.get("HSCode")),
            "conversion_price": parse_float(record.get("ZGPrice")),
            "remaining_size": parse_float(record.get("RestScope")),
            "maturity_date": parse_date(record.get("EndDate")),
            "bond_price": parse_float(record.get("KZZPrice")),
            "stock_price": parse_float(record.get("AGPrice")),
            "premium_rate": parse_float(record.get("KZZYj")),
            "conversion_value": parse_float(record.get("ZGValue")),
            "data_date": data_date,
            "updated_at": now_text(),
        })
    return rows


def build_etf_rows(
    records: list[Mapping[str, Any]] | None,
    data_date: str,
) -> list[dict[str, Any]]:
    # 将指数跟踪 ETF 接口结果转换为当前表记录。
    rows: list[dict[str, Any]] = []
    for record in records or []:
        index_code = normalize_optional_code(record.get("_index_code"))
        etf_code = normalize_code(record.get("Code"))
        if not index_code or not etf_code:
            continue
        rows.append({
            "index_code": index_code,
            "etf_code": etf_code,
            "etf_name": clean_name(record.get("Name")),
            "current_price": parse_float(record.get("NowPrice")),
            "previous_close": parse_float(record.get("PreClose")),
            "iopv": parse_float(record.get("IOPV")),
            "shares_10k": parse_float(record.get("Zgb")),
            "size_100m": parse_float(record.get("Sz")),
            "data_date": data_date,
            "updated_at": now_text(),
        })
    return rows


def validate_daily_rows(
    rows: list[Mapping[str, Any]],
    expected_codes: set[str],
    previous_count: int = 0,
) -> list[str]:
    # 校验每日股票宽表是否满足发布要求。
    errors: list[str] = []
    codes = [row.get("stock_code") for row in rows]
    code_set = set(codes)
    if not expected_codes:
        errors.append("所有 A 股分类为空")
    if len(codes) != len(code_set):
        errors.append("每日宽表存在重复股票代码")
    if code_set != expected_codes:
        errors.append("每日宽表没有覆盖全部历史股票")
    active_count = sum(row.get("is_active") == 1 for row in rows)
    if previous_count and active_count < previous_count * 0.85:
        errors.append("当前活动股票数量较上一日下降超过 15%")
    flag_fields = (*POOL_FLAG_FIELDS, "is_active", "is_st", "is_suspended")
    for row in rows:
        stock_code = row.get("stock_code") or "UNKNOWN"
        for field_name in flag_fields:
            if row.get(field_name) not in {0, 1, None}:
                errors.append(f"{stock_code} 的 {field_name} 不是有效标识")
        for field_name in (
            "total_shares_10k",
            "float_shares_10k",
            "total_market_cap_100m",
            "float_market_cap_100m",
        ):
            value = row.get(field_name)
            if value is not None and value < 0:
                errors.append(f"{stock_code} 的 {field_name} 为负数")
    return errors
