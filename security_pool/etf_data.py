"""ETF 主数据、历史行情、净值、份额和规模的轻量采集器。"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from stock_data.fetcher import fetch_daily_bars, to_tdx_date
from stock_data.processor import flatten_daily_bars

from . import db
from .fetcher import call_with_retry, init_tdx
from .processor import clean_name, normalize_code, now_text, parse_date, parse_flag, parse_float


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "security_pool.db"
ETF_METRIC_FIELDS = ("GP47", "GP51", "GP52")

ETF_MASTER_COLUMNS = (
    "etf_code", "etf_name", "exchange", "list_date", "underlying_code",
    "underlying_market_code", "is_t0", "is_marginable", "trade_unit",
    "price_precision", "total_shares_10k", "first_seen_date", "raw_info_json",
    "created_at", "updated_at",
)

ETF_DAILY_COLUMNS = (
    "etf_code", "trade_date", "open", "high", "low", "close", "pre_close",
    "pct_change", "volume", "amount_10k", "is_trading", "forward_factor",
    "net_subscription_10k", "unit_nav", "cumulative_nav", "shares_10k",
    "size_10k", "premium_discount_pct", "raw_metrics_json", "updated_at",
)

ETF_MARKET_COLUMNS = (
    "trade_date", "total_shares_100m", "net_subscription_shares_100m",
    "total_size_100m", "net_subscription_size_100m", "raw_metrics_json",
    "updated_at",
)


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _member_map(value: Any) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for item in _records(value):
        code = normalize_code(item.get("Code"))
        if code:
            result[code] = clean_name(item.get("Name"))
    return result


def _underlying_code(info: Mapping[str, Any]) -> str | None:
    raw = str(info.get("underly_code") or "").strip().upper()
    if not raw or raw == "0":
        return None
    if "." in raw:
        return raw
    suffix = {"0": "SZ", "1": "SH", "12": "OT"}.get(
        str(info.get("underly_setcode") or "").strip()
    )
    return f"{raw}.{suffix}" if suffix else raw


def fetch_etf_master_rows(client: Any, data_date: str) -> list[dict[str, Any]]:
    # ETF分类覆盖所有ETF；T+0分类只用于设置交易制度标记。
    members = _member_map(call_with_retry(client.get_stock_list, "31", 1))
    t0_codes = set(_member_map(call_with_retry(client.get_stock_list, "36", 1)))
    timestamp = now_text()
    rows: list[dict[str, Any]] = []
    for index, (etf_code, category_name) in enumerate(sorted(members.items()), start=1):
        try:
            raw_info = call_with_retry(client.get_stock_info, etf_code, [])
            info = raw_info if isinstance(raw_info, Mapping) else {}
        except Exception as exc:
            LOGGER.warning("ETF %s 基本信息获取失败，保留分类信息: %s", etf_code, exc)
            info = {}
        rows.append({
            "etf_code": etf_code,
            "etf_name": clean_name(info.get("Name")) or category_name,
            "exchange": etf_code.rsplit(".", 1)[-1],
            "list_date": parse_date(info.get("J_start")),
            "underlying_code": _underlying_code(info),
            "underlying_market_code": (
                str(info.get("underly_setcode"))
                if info.get("underly_setcode") not in {None, ""}
                else None
            ),
            "is_t0": int(etf_code in t0_codes),
            "is_marginable": parse_flag(info.get("BelongRZRQ")),
            "trade_unit": parse_float(info.get("Unit")),
            "price_precision": int(parse_float(info.get("XsFlag")) or 0) or None,
            "total_shares_10k": parse_float(info.get("ActiveCapital") or info.get("J_zgb")),
            "first_seen_date": data_date,
            "raw_info_json": json.dumps(dict(info), ensure_ascii=False, sort_keys=True, default=str),
            "created_at": timestamp,
            "updated_at": timestamp,
        })
        if index % 100 == 0 or index == len(members):
            LOGGER.info("ETF主数据进度 %s/%s", index, len(members))
    return rows


def upsert_etf_master(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    names = ",".join(ETF_MASTER_COLUMNS)
    placeholders = ",".join("?" for _ in ETF_MASTER_COLUMNS)
    updates = ",".join(
        f"{column}=excluded.{column}"
        for column in ETF_MASTER_COLUMNS
        if column not in {"etf_code", "first_seen_date", "created_at"}
    )
    sql = (
        f"INSERT INTO etf_master({names}) VALUES({placeholders}) "
        f"ON CONFLICT(etf_code) DO UPDATE SET {updates}"
    )
    connection.executemany(
        sql,
        (tuple(row.get(column) for column in ETF_MASTER_COLUMNS) for row in rows),
    )
    return len(rows)


def _metric_rows(raw: Any) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(raw, Mapping):
        return []
    for raw_code, fields in raw.items():
        code = normalize_code(raw_code)
        if not code or not isinstance(fields, Mapping):
            continue
        for field, observations in fields.items():
            for observation in observations if isinstance(observations, (list, tuple)) else []:
                if not isinstance(observation, Mapping):
                    continue
                trade_date = parse_date(observation.get("Date"))
                values = observation.get("Value")
                if not trade_date or not isinstance(values, (list, tuple)):
                    continue
                row = result.setdefault(
                    (code, trade_date),
                    {"etf_code": code, "trade_date": trade_date, "_raw": {}},
                )
                parsed = [parse_float(value) for value in values]
                row["_raw"][str(field).upper()] = parsed
                if str(field).upper() == "GP47":
                    row["net_subscription_10k"] = parsed[0] if parsed else None
                elif str(field).upper() == "GP51":
                    row["unit_nav"] = parsed[0] if parsed else None
                    row["cumulative_nav"] = parsed[1] if len(parsed) > 1 else None
                elif str(field).upper() == "GP52":
                    row["shares_10k"] = parsed[0] if parsed else None
                    row["size_10k"] = parsed[1] if len(parsed) > 1 else None
    output = []
    for key in sorted(result):
        row = result[key]
        row["raw_metrics_json"] = json.dumps(row.pop("_raw"), separators=(",", ":"), sort_keys=True)
        output.append(row)
    return output


def _merge_daily_rows(
    bar_rows: Sequence[Mapping[str, Any]],
    metric_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for source in (*bar_rows, *metric_rows):
        code = normalize_code(source.get("stock_code") or source.get("etf_code"))
        trade_date = parse_date(source.get("trade_date"))
        if not code or not trade_date:
            continue
        row = merged.setdefault(
            (code, trade_date),
            {"etf_code": code, "trade_date": trade_date, "raw_metrics_json": "{}"},
        )
        for key, value in source.items():
            if key not in {"stock_code", "etf_code", "trade_date"} and value is not None:
                row[key] = value
    timestamp = now_text()
    for row in merged.values():
        close = row.get("close")
        nav = row.get("unit_nav")
        if close is not None and nav not in {None, 0}:
            row["premium_discount_pct"] = (float(close) / float(nav) - 1.0) * 100.0
        row["updated_at"] = timestamp
    return [merged[key] for key in sorted(merged)]


def upsert_etf_daily(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    # 保存行情、复权因子与可获得的采集时间，保留已有非空历史值。
    if not rows:
        return 0
    names = ",".join(ETF_DAILY_COLUMNS)
    placeholders = ",".join("?" for _ in ETF_DAILY_COLUMNS)
    updates = []
    for column in ETF_DAILY_COLUMNS:
        if column in {"etf_code", "trade_date"}:
            continue
        if column == "raw_metrics_json":
            updates.append(
                "raw_metrics_json=json_patch(COALESCE(etf_daily.raw_metrics_json,'{}'),excluded.raw_metrics_json)"
            )
        elif column == "updated_at":
            updates.append("updated_at=excluded.updated_at")
        else:
            updates.append(f"{column}=COALESCE(excluded.{column},etf_daily.{column})")
    sql = (
        f"INSERT INTO etf_daily({names}) VALUES({placeholders}) "
        f"ON CONFLICT(etf_code,trade_date) DO UPDATE SET {','.join(updates)}"
    )
    connection.executemany(
        sql,
        (tuple(row.get(column) for column in ETF_DAILY_COLUMNS) for row in rows),
    )
    evidence = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='etf_daily_provenance'"
    ).fetchone()
    if evidence:
        provenance = []
        for row in rows:
            for domain, key in (("bar", "close"), ("nav", "unit_nav"),
                                ("size", "size_10k"), ("adjustment", "forward_factor")):
                if row.get(key) is not None:
                    provenance.append((row["etf_code"], row["trade_date"], domain,
                                       None, row["updated_at"], "tdx"))
        connection.executemany(
            "INSERT OR REPLACE INTO etf_daily_provenance VALUES(?,?,?,?,?,?)", provenance
        )
    return len(rows)


def _save_sync_state(
    connection: sqlite3.Connection,
    codes: Sequence[str],
    end_date: str,
) -> None:
    connection.executemany(
        "INSERT INTO etf_data_sync_state(etf_code,data_domain,last_success_date,updated_at) "
        "VALUES(?,'daily',?,?) ON CONFLICT(etf_code,data_domain) DO UPDATE SET "
        "last_success_date=MAX(etf_data_sync_state.last_success_date,excluded.last_success_date),"
        "updated_at=excluded.updated_at",
        ((code, end_date, now_text()) for code in codes),
    )


def _market_rows(raw: Any) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, Mapping):
        return []
    for field, observations in raw.items():
        for observation in observations if isinstance(observations, (list, tuple)) else []:
            if not isinstance(observation, Mapping):
                continue
            trade_date = parse_date(observation.get("Date"))
            values = observation.get("Value")
            if not trade_date or not isinstance(values, (list, tuple)):
                continue
            parsed = [parse_float(value) for value in values]
            row = records.setdefault(trade_date, {"trade_date": trade_date, "_raw": {}})
            row["_raw"][str(field).upper()] = parsed
            if str(field).upper() == "SC08":
                row["total_shares_100m"] = parsed[0] if parsed else None
                row["net_subscription_shares_100m"] = parsed[1] if len(parsed) > 1 else None
            elif str(field).upper() == "SC38":
                row["total_size_100m"] = parsed[0] if parsed else None
                row["net_subscription_size_100m"] = parsed[1] if len(parsed) > 1 else None
    timestamp = now_text()
    output = []
    for trade_date in sorted(records):
        row = records[trade_date]
        row["raw_metrics_json"] = json.dumps(row.pop("_raw"), separators=(",", ":"), sort_keys=True)
        row["updated_at"] = timestamp
        output.append(row)
    return output


def _save_market_rows(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    names = ",".join(ETF_MARKET_COLUMNS)
    placeholders = ",".join("?" for _ in ETF_MARKET_COLUMNS)
    updates = ",".join(
        f"{column}=excluded.{column}" for column in ETF_MARKET_COLUMNS if column != "trade_date"
    )
    connection.executemany(
        f"INSERT INTO etf_market_daily({names}) VALUES({placeholders}) "
        f"ON CONFLICT(trade_date) DO UPDATE SET {updates}",
        (tuple(row.get(column) for column in ETF_MARKET_COLUMNS) for row in rows),
    )
    return len(rows)


def load_etf_data(
    end_date: str,
    start_date: str = "2004-01-01",
    db_path: str | Path = DEFAULT_DB_PATH,
    batch_size: int = 5,
    refresh_master: bool = True,
    incremental: bool = False,
    client: Any = None,
    api: Any = None,
) -> dict[str, Any]:
    # 可恢复地补齐 ETF 主数据、日线和 GP47/51/52 历史数据。
    date.fromisoformat(start_date)
    date.fromisoformat(end_date)
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    connection = db.connect(db_path)
    db.init_db(connection)
    log_id = db.start_log(connection, end_date, "ETF_DATA_FULL")
    active_client = client or init_tdx(api=api, script_file=__file__)
    errors: dict[str, str] = {}
    written_daily = 0
    try:
        if refresh_master or not connection.execute("SELECT 1 FROM etf_master LIMIT 1").fetchone():
            master_rows = fetch_etf_master_rows(active_client, end_date)
            with connection:
                upsert_etf_master(connection, master_rows)
        master_data = {
            str(row["etf_code"]): dict(row)
            for row in connection.execute("SELECT etf_code,list_date FROM etf_master")
        }
        codes = sorted(master_data)
        sync_dates = {
            str(row["etf_code"]): str(row["last_success_date"])
            for row in connection.execute(
                "SELECT etf_code,last_success_date FROM etf_data_sync_state "
                "WHERE data_domain='daily'"
            )
        }

        # 将现有 ETF 当前映射补归档，避免首次扩展丢失上一交易日快照。
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO etf_snapshot(snapshot_date,index_code,etf_code,etf_name,"
                "current_price,previous_close,iopv,shares_10k,size_100m,premium_discount_pct,archived_at) "
                "SELECT data_date,index_code,etf_code,etf_name,current_price,previous_close,iopv,"
                "shares_10k,size_100m,CASE WHEN iopv<>0 THEN (current_price/iopv-1)*100 END,updated_at "
                "FROM etf_info"
            )

        for offset in range(0, len(codes), batch_size):
            batch = codes[offset:offset + batch_size]
            try:
                batch_start = start_date
                if incremental:
                    batch_start = min(
                        min(
                            sync_dates.get(code)
                            or master_data[code].get("list_date")
                            or end_date,
                            end_date,
                        )
                        for code in batch
                    )
                raw_bars = fetch_daily_bars(active_client, batch, batch_start, end_date)
                bar_rows = flatten_daily_bars(raw_bars)
                raw_metrics = call_with_retry(
                    active_client.get_gpjy_value,
                    batch,
                    list(ETF_METRIC_FIELDS),
                    to_tdx_date(batch_start),
                    to_tdx_date(end_date),
                )
                rows = _merge_daily_rows(bar_rows, _metric_rows(raw_metrics))
                with connection:
                    written_daily += upsert_etf_daily(connection, rows)
                    _save_sync_state(connection, batch, end_date)
            except Exception as exc:
                LOGGER.exception("ETF批次 %s 获取失败", batch)
                errors.update({code: str(exc) for code in batch})
            completed = min(offset + batch_size, len(codes))
            if completed % 100 < batch_size or completed == len(codes):
                LOGGER.info("ETF历史数据进度 %s/%s", completed, len(codes))

        try:
            raw_market = call_with_retry(
                active_client.get_scjy_value,
                ["SC08", "SC38"],
                to_tdx_date(start_date),
                to_tdx_date(end_date),
            )
            with connection:
                market_count = _save_market_rows(connection, _market_rows(raw_market))
        except Exception as exc:
            LOGGER.warning("全市场ETF指标获取失败: %s", exc)
            market_count = 0

        status = "SUCCESS" if not errors else "PARTIAL"
        db.finish_log(
            connection,
            log_id,
            status,
            stock_count=len(codes) - len(errors),
            error_message=(json.dumps(errors, ensure_ascii=False) if errors else None),
        )
        return {
            "status": status,
            "etf_count": len(codes),
            "succeeded_etfs": len(codes) - len(errors),
            "written_daily_rows": written_daily,
            "market_daily_rows": market_count,
            "errors": errors,
        }
    except Exception as exc:
        connection.rollback()
        db.finish_log(connection, log_id, "FAILED", error_message=str(exc))
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="通达信ETF扩展数据采集")
    parser.add_argument("command", choices=("full-load", "daily-update"))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--skip-master", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    start_date = args.start_date or (
        args.end_date if args.command == "daily-update" else "2004-01-01"
    )
    print(load_etf_data(
        args.end_date,
        start_date,
        args.db,
        args.batch_size,
        not args.skip_master,
        args.command == "daily-update",
    ))


if __name__ == "__main__":
    main()
