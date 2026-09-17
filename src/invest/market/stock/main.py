"""股票详细数据全量、增量、修复和校验命令。"""

from __future__ import annotations

from invest.core.settings import ROOT as PROJECT_HOME
from invest.core.settings import load_settings

import argparse
import logging
from collections.abc import Sequence
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any

from invest.providers.tdx_client import TdxClient

from invest.storage import stock as db
from invest.providers.stock import CORE_FN_FIELDS
from invest.providers.stock import FN_FIELDS
from invest.providers.stock import fetch_capital_history
from invest.providers.stock import fetch_corporate_actions
from invest.providers.stock import fetch_daily_bars
from invest.providers.stock import fetch_financial_reports
from invest.providers.stock import fetch_trade_metrics
from invest.providers.stock import init_tdx
from invest.market.stock.processor import build_action_rows
from invest.market.stock.processor import build_capital_rows
from invest.market.stock.processor import build_financial_rows
from invest.market.stock.processor import flatten_daily_bars
from invest.market.stock.processor import pivot_trade_metrics
from invest.market.stock.processor import validate_bar_rows
from invest.market.stock.processor import validate_capital_rows
from invest.market.stock.processor import validate_financial_rows


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = PROJECT_HOME
DEFAULT_DB_PATH = load_settings().databases["market"]
ALL_DOMAINS = ("bar", "capital", "action", "trade", "financial")


def configure_logging(level: int = logging.INFO) -> None:
    # 配置股票详细数据命令行日志。
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def normalize_date(value: str | date | None) -> str:
    # 将可选日期转换为 ISO 日期。
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()


def shift_days(value: str, days: int) -> str:
    # 将 ISO 日期移动指定自然日数。
    return (date.fromisoformat(value) + timedelta(days=days)).isoformat()


def parse_stock_codes(value: str | None) -> list[str] | None:
    # 解析逗号分隔的标准股票代码列表。
    if not value:
        return None
    codes = [item.strip().upper() for item in value.split(",") if item.strip()]
    return list(dict.fromkeys(codes)) or None


def parse_domains(value: str | None) -> tuple[str, ...]:
    # 解析并验证逗号分隔的数据域列表。
    if not value:
        return ALL_DOMAINS
    domains = tuple(dict.fromkeys(item.strip() for item in value.split(",")))
    invalid = [item for item in domains if item not in ALL_DOMAINS]
    if invalid:
        raise ValueError(f"未知数据域: {','.join(invalid)}")
    return domains


def init_database(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    # 创建证券池和股票详细数据表。
    connection = db.connect(db_path)
    try:
        db.init_db(connection)
        return db.table_counts(connection)
    finally:
        connection.close()


def _resolve_client(client: TdxClient | None, api: Any) -> TdxClient:
    # 使用注入客户端或创建真实通达信客户端。
    return client if client is not None else init_tdx(api=api, script_file=__file__)


def _load_scope(
    connection: Any,
    stock_codes: Sequence[str] | None,
    active_only: bool,
    shard_index: int = 0,
    shard_count: int = 1,
) -> list[dict[str, Any]]:
    # 加载股票范围、检查指定代码并按稳定顺序切分任务分片。
    if shard_count <= 0:
        raise ValueError("分片总数必须大于零")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("分片序号必须在零到分片总数减一之间")
    scope = db.load_stock_scope(connection, stock_codes, active_only=active_only)
    if stock_codes:
        found = {row["stock_code"] for row in scope}
        missing = sorted(set(stock_codes) - found)
        if missing:
            raise ValueError(f"股票主表不存在代码: {','.join(missing)}")
    if not scope:
        raise RuntimeError("股票数据获取范围为空")
    sharded_scope = [
        stock for index, stock in enumerate(scope)
        if index % shard_count == shard_index
    ]
    if not sharded_scope:
        raise RuntimeError("当前股票数据分片为空")
    return sharded_scope


def _apply_previous_close(connection: Any, rows: list[dict[str, Any]]) -> None:
    # 为增量窗口首条 K 线补充数据库中的上一收盘价。
    if not rows:
        return
    first = min(rows, key=lambda row: row["trade_date"])
    stock_code = first["stock_code"]
    trade_date = first["trade_date"]
    result = connection.execute(
        "SELECT close FROM stock_daily_bar "
        "WHERE stock_code=? AND trade_date<? AND close IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1",
        (stock_code, trade_date),
    ).fetchone()
    if result is None or result[0] in {None, 0}:
        return
    previous_close = float(result[0])
    first["pre_close"] = previous_close
    if first.get("close") is not None:
        first["pct_change"] = (first["close"] / previous_close - 1.0) * 100.0


def _fetch_rows(
    client: TdxClient,
    stock_code: str,
    starts: dict[str, str],
    end_date: str,
    domains: Sequence[str],
    financial_fields: Sequence[str] = CORE_FN_FIELDS,
) -> dict[str, list[dict[str, Any]]]:
    # 获取并转换一只股票在各数据域的记录。
    rows: dict[str, list[dict[str, Any]]] = {
        domain: [] for domain in ALL_DOMAINS
    }
    if "bar" in domains:
        raw_bars = fetch_daily_bars(
            client,
            [stock_code],
            starts["bar"],
            end_date,
        )
        rows["bar"] = flatten_daily_bars(raw_bars)
    if "capital" in domains:
        raw_capital = fetch_capital_history(
            client,
            stock_code,
            starts["capital"],
            end_date,
        )
        rows["capital"] = build_capital_rows(stock_code, raw_capital)
    if "action" in domains:
        raw_actions = fetch_corporate_actions(
            client,
            stock_code,
            starts["action"],
            end_date,
        )
        rows["action"] = build_action_rows(stock_code, raw_actions)
    if "trade" in domains:
        raw_trade = fetch_trade_metrics(
            client,
            [stock_code],
            starts["trade"],
            end_date,
        )
        rows["trade"] = pivot_trade_metrics(raw_trade)
    if "financial" in domains:
        raw_financial = fetch_financial_reports(
            client,
            [stock_code],
            starts["financial"],
            end_date,
            fields=financial_fields,
        )
        rows["financial"] = build_financial_rows(raw_financial)
    return rows


def _validate_rows(rows: dict[str, list[dict[str, Any]]]) -> None:
    # 汇总一只股票各数据域的业务校验错误。
    errors = [
        *validate_bar_rows(rows["bar"]),
        *validate_capital_rows(rows["capital"]),
        *validate_financial_rows(rows["financial"]),
    ]
    if errors:
        raise RuntimeError("；".join(errors[:20]))


def _save_rows(
    connection: Any,
    rows: dict[str, list[dict[str, Any]]],
    stock_code: str,
    domains: Sequence[str],
    end_date: str,
) -> dict[str, int]:
    # 在单个事务中写入一只股票的全部已获取数据域。
    _apply_previous_close(connection, rows["bar"])
    with connection:
        counts = {
            "bar": db.upsert_daily_bars(connection, rows["bar"]),
            "capital": db.upsert_capital_rows(connection, rows["capital"]),
            "action": db.upsert_action_rows(connection, rows["action"]),
            "trade": db.upsert_trade_rows(connection, rows["trade"]),
            "financial": db.upsert_financial_rows(
                connection,
                rows["financial"],
            ),
        }
        db.save_sync_state(connection, [stock_code], domains, end_date)
        return counts


def _full_starts(stock: dict[str, Any], start_date: str | None) -> dict[str, str]:
    # 生成从上市日开始的全量数据域起始日期。
    first_date = start_date or stock.get("list_date") or "1990-01-01"
    return {domain: normalize_date(first_date) for domain in ALL_DOMAINS}


def _daily_starts(
    connection: Any,
    stock: dict[str, Any],
    end_date: str,
) -> dict[str, str]:
    # 从上次成功同步截止日（含当日）开始增量回取各数据域。
    fallback_date = normalize_date(stock.get("list_date") or end_date)
    return db.load_incremental_starts(
        connection,
        stock["stock_code"],
        fallback_date,
        end_date,
    )


def _scope_batches(
    scope: Sequence[dict[str, Any]],
    batch_size: int,
) -> list[list[dict[str, Any]]]:
    # 将股票范围拆成固定数量的批次。
    if batch_size <= 0:
        raise ValueError("股票批次大小必须大于零")
    return [
        list(scope[index:index + batch_size])
        for index in range(0, len(scope), batch_size)
    ]


def _stock_start(stock: dict[str, Any], start_date: str | None) -> str:
    # 返回不早于指定下限且不早于上市日的单股起始日期。
    list_date = normalize_date(stock.get("list_date") or "1990-01-01")
    return max(list_date, normalize_date(start_date)) if start_date else list_date


def _batch_start(
    batch: Sequence[dict[str, Any]],
    start_date: str | None = None,
) -> str:
    # 返回股票批次中不早于指定下限的最早起始日期。
    return min(_stock_start(stock, start_date) for stock in batch)


def _save_base_batch(
    connection: Any,
    client: TdxClient,
    batch: Sequence[dict[str, Any]],
    end_date: str,
    start_date: str | None,
    domains: Sequence[str],
) -> dict[str, int]:
    # 按指定数据域获取 K 线、股本和公司行为并统一写入。
    stock_codes = [stock["stock_code"] for stock in batch]
    bar_rows: list[dict[str, Any]] = []
    if "bar" in domains:
        raw_bars = fetch_daily_bars(
            client,
            stock_codes,
            _batch_start(batch, start_date),
            end_date,
        )
        bar_rows = flatten_daily_bars(raw_bars)
    capital_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    for stock in batch:
        stock_code = stock["stock_code"]
        stock_start = _stock_start(stock, start_date)
        if "capital" in domains:
            raw_capital = fetch_capital_history(
                client,
                stock_code,
                stock_start,
                end_date,
            )
            capital_rows.extend(build_capital_rows(stock_code, raw_capital))
        if "action" in domains:
            raw_actions = fetch_corporate_actions(
                client,
                stock_code,
                stock_start,
                end_date,
            )
            action_rows.extend(build_action_rows(stock_code, raw_actions))
    errors: list[str] = []
    if "bar" in domains:
        errors.extend(validate_bar_rows(bar_rows))
    if "capital" in domains:
        errors.extend(validate_capital_rows(capital_rows))
    if errors:
        raise RuntimeError("；".join(errors[:20]))
    with connection:
        counts: dict[str, int] = {}
        if "bar" in domains:
            counts["bar"] = db.upsert_daily_bars(connection, bar_rows)
        if "capital" in domains:
            counts["capital"] = db.upsert_capital_rows(connection, capital_rows)
        if "action" in domains:
            counts["action"] = db.upsert_action_rows(connection, action_rows)
        db.save_sync_state(connection, stock_codes, domains, end_date)
        return counts


def _save_metric_batch(
    connection: Any,
    client: TdxClient,
    batch: Sequence[dict[str, Any]],
    end_date: str,
    start_date: str | None,
    domains: Sequence[str],
    financial_fields: Sequence[str] = CORE_FN_FIELDS,
) -> dict[str, int]:
    # 按指定数据域获取并写入 GP 交易指标或专业财务报告。
    stock_codes = [stock["stock_code"] for stock in batch]
    batch_start = _batch_start(batch, start_date)
    trade_rows: list[dict[str, Any]] = []
    financial_rows: list[dict[str, Any]] = []
    if "trade" in domains:
        raw_trade = fetch_trade_metrics(
            client,
            stock_codes,
            batch_start,
            end_date,
        )
        trade_rows = pivot_trade_metrics(raw_trade)
    if "financial" in domains:
        raw_financial = fetch_financial_reports(
            client,
            stock_codes,
            batch_start,
            end_date,
            fields=financial_fields,
        )
        financial_rows = build_financial_rows(raw_financial)
        errors = validate_financial_rows(financial_rows)
        if errors:
            raise RuntimeError("；".join(errors[:20]))
    with connection:
        counts: dict[str, int] = {}
        if "trade" in domains:
            counts["trade"] = db.upsert_trade_rows(connection, trade_rows)
        if "financial" in domains:
            counts["financial"] = db.upsert_financial_rows(
                connection,
                financial_rows,
            )
        db.save_sync_state(connection, stock_codes, domains, end_date)
        return counts


def _record_batch_error(
    errors: dict[str, str],
    batch: Sequence[dict[str, Any]],
    domain: str,
    error: Exception,
) -> None:
    # 将批次错误记录到批次内每只股票。
    for stock in batch:
        stock_code = stock["stock_code"]
        previous = errors.get(stock_code)
        message = f"{domain}: {error}"
        errors[stock_code] = f"{previous}；{message}" if previous else message


def bulk_full_load(
    end_date: str | date | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    stock_codes: Sequence[str] | None = None,
    bar_batch_size: int = 3,
    metric_batch_size: int = 10,
    start_date: str | None = None,
    domains: Sequence[str] = ALL_DOMAINS,
    financial_fields: Sequence[str] = CORE_FN_FIELDS,
    client: TdxClient | None = None,
    api: Any = None,
) -> dict[str, Any]:
    # 按起始日期和数据域分批执行全市场历史数据获取。
    task_date = normalize_date(end_date)
    normalized_start = normalize_date(start_date) if start_date else None
    requested_domains = tuple(dict.fromkeys(domains))
    invalid_domains = [
        domain for domain in requested_domains if domain not in ALL_DOMAINS
    ]
    if invalid_domains:
        raise ValueError(f"未知数据域: {','.join(invalid_domains)}")
    if not requested_domains:
        raise ValueError("至少需要指定一个数据域")
    if requested_domains == ("financial",):
        task_type = (
            "STOCK_DATA_BULK_FINANCIAL_CORE"
            if tuple(financial_fields) == CORE_FN_FIELDS
            else "STOCK_DATA_BULK_FINANCIAL"
        )
    elif "financial" not in requested_domains:
        task_type = "STOCK_DATA_BULK_STAGE1"
    else:
        task_type = "STOCK_DATA_BULK_FULL"
    connection = db.connect(db_path)
    db.init_db(connection)
    log_id = db.start_log(connection, task_date, task_type)
    errors: dict[str, str] = {}
    written = {domain: 0 for domain in ALL_DOMAINS}
    try:
        scope = _load_scope(connection, stock_codes, False)
        active_client = _resolve_client(client, api)
        scope_codes = {stock["stock_code"] for stock in scope}
        base_domains = tuple(
            domain
            for domain in requested_domains
            if domain in {"bar", "capital", "action"}
        )
        base_success: set[str] = set(scope_codes) if not base_domains else set()
        base_batches = _scope_batches(scope, bar_batch_size) if base_domains else []
        for index, batch in enumerate(base_batches, start=1):
            try:
                counts = _save_base_batch(
                    connection,
                    active_client,
                    batch,
                    task_date,
                    normalized_start,
                    base_domains,
                )
                for domain, count in counts.items():
                    written[domain] += count
                base_success.update(stock["stock_code"] for stock in batch)
            except Exception as exc:
                _record_batch_error(errors, batch, "BASE", exc)
                LOGGER.exception("基础数据批次 %s 获取失败", index)
            if index % 20 == 0 or index == len(base_batches):
                LOGGER.info(
                    "基础数据批次进度 %s/%s，成功股票 %s",
                    index,
                    len(base_batches),
                    len(base_success),
                )

        metric_domains = tuple(
            domain
            for domain in requested_domains
            if domain in {"trade", "financial"}
        )
        metric_success: set[str] = (
            set(scope_codes) if not metric_domains else set()
        )
        metric_batches = (
            _scope_batches(scope, metric_batch_size) if metric_domains else []
        )
        for index, batch in enumerate(metric_batches, start=1):
            try:
                counts = _save_metric_batch(
                    connection,
                    active_client,
                    batch,
                    task_date,
                    normalized_start,
                    metric_domains,
                    financial_fields,
                )
                for domain, count in counts.items():
                    written[domain] += count
                metric_success.update(stock["stock_code"] for stock in batch)
            except Exception as exc:
                _record_batch_error(errors, batch, "METRIC", exc)
                LOGGER.exception("指标数据批次 %s 获取失败", index)
            if index % 10 == 0 or index == len(metric_batches):
                LOGGER.info(
                    "指标数据批次进度 %s/%s，成功股票 %s",
                    index,
                    len(metric_batches),
                    len(metric_success),
                )

        succeeded = base_success & metric_success
        status = "SUCCESS" if len(succeeded) == len(scope) else "PARTIAL_SUCCESS"
        if not succeeded:
            status = "FAILED"
        error_message = None
        if errors:
            error_message = "；".join(
                f"{code}: {message}" for code, message in errors.items()
            )[:4000]
        db.finish_log(
            connection,
            log_id,
            status,
            stock_count=len(succeeded),
            error_message=error_message,
        )
        result = {
            "status": status,
            "requested_stocks": len(scope),
            "succeeded_stocks": len(succeeded),
            "start_date": normalized_start,
            "domains": requested_domains,
            "financial_field_count": (
                len(financial_fields) if "financial" in requested_domains else 0
            ),
            "written_rows": written,
            "table_counts": db.table_counts(connection, stock_codes),
            "errors": errors,
        }
        if status == "FAILED":
            raise RuntimeError(error_message or "全市场批处理全部失败")
        return result
    except KeyboardInterrupt:
        connection.rollback()
        db.finish_log(
            connection,
            log_id,
            "INTERRUPTED",
            stock_count=0,
            error_message="用户或调度器中断任务",
        )
        raise
    except Exception as exc:
        connection.rollback()
        db.finish_log(
            connection,
            log_id,
            "FAILED",
            stock_count=0,
            error_message=str(exc)[:4000],
        )
        raise
    finally:
        connection.close()


def _run_update(
    task_type: str,
    end_date: str | date | None,
    db_path: str | Path,
    stock_codes: Sequence[str] | None,
    domains: Sequence[str],
    start_date: str | None = None,
    active_only: bool = False,
    shard_index: int = 0,
    shard_count: int = 1,
    client: TdxClient | None = None,
    api: Any = None,
    financial_fields: Sequence[str] = CORE_FN_FIELDS,
) -> dict[str, Any]:
    # 编排多股票全量、增量或修复任务并记录部分失败。
    task_date = normalize_date(end_date)
    connection = db.connect(db_path)
    db.init_db(connection)
    log_id = db.start_log(connection, task_date, task_type)
    succeeded: list[str] = []
    errors: dict[str, str] = {}
    written = {domain: 0 for domain in ALL_DOMAINS}
    try:
        scope = _load_scope(
            connection,
            stock_codes,
            active_only,
            shard_index,
            shard_count,
        )
        active_client = _resolve_client(client, api)
        for index, stock in enumerate(scope, start=1):
            stock_code = stock["stock_code"]
            try:
                starts = (
                    _daily_starts(connection, stock, task_date)
                    if task_type == "STOCK_DATA_DAILY"
                    else _full_starts(stock, start_date)
                )
                rows = _fetch_rows(
                    active_client,
                    stock_code,
                    starts,
                    task_date,
                    domains,
                    financial_fields,
                )
                _validate_rows(rows)
                counts = _save_rows(
                    connection,
                    rows,
                    stock_code,
                    domains,
                    task_date,
                )
                for domain, count in counts.items():
                    written[domain] += count
                succeeded.append(stock_code)
                LOGGER.info(
                    "股票详细数据进度 %s/%s %s: %s",
                    index,
                    len(scope),
                    stock_code,
                    counts,
                )
            except Exception as exc:
                errors[stock_code] = str(exc)
                LOGGER.exception("股票 %s 详细数据获取失败", stock_code)

        if not succeeded:
            status = "FAILED"
        elif errors:
            status = "PARTIAL_SUCCESS"
        else:
            status = "SUCCESS"
        error_message = None
        if errors:
            error_message = "；".join(
                f"{code}: {message}" for code, message in errors.items()
            )[:4000]
        db.finish_log(
            connection,
            log_id,
            status,
            stock_count=len(succeeded),
            error_message=error_message,
        )
        result = {
            "status": status,
            "requested_stocks": len(scope),
            "succeeded_stocks": len(succeeded),
            "written_rows": written,
            "table_counts": db.table_counts(connection, stock_codes),
            "errors": errors,
        }
        if status == "FAILED":
            raise RuntimeError(error_message or "全部股票详细数据获取失败")
        return result
    except KeyboardInterrupt:
        connection.rollback()
        db.finish_log(
            connection,
            log_id,
            "INTERRUPTED",
            stock_count=len(succeeded),
            error_message="用户或调度器中断任务",
        )
        raise
    except Exception as exc:
        connection.rollback()
        if not succeeded:
            db.finish_log(
                connection,
                log_id,
                "FAILED",
                stock_count=0,
                error_message=str(exc)[:4000],
            )
        raise
    finally:
        connection.close()


def full_load(
    end_date: str | date | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    stock_codes: Sequence[str] | None = None,
    client: TdxClient | None = None,
    api: Any = None,
    shard_index: int = 0,
    shard_count: int = 1,
    financial_fields: Sequence[str] = CORE_FN_FIELDS,
) -> dict[str, Any]:
    # 从各股票上市日获取第一批和第二批全部历史数据。
    return _run_update(
        "STOCK_DATA_FULL",
        end_date,
        db_path,
        stock_codes,
        ALL_DOMAINS,
        shard_index=shard_index,
        shard_count=shard_count,
        client=client,
        api=api,
        financial_fields=financial_fields,
    )


def daily_update(
    data_date: str | date | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    stock_codes: Sequence[str] | None = None,
    domains: Sequence[str] = ALL_DOMAINS,
    client: TdxClient | None = None,
    api: Any = None,
    financial_fields: Sequence[str] = CORE_FN_FIELDS,
) -> dict[str, Any]:
    # 对活动股票执行按上次成功截止日续取的每日详细数据更新。
    return _run_update(
        "STOCK_DATA_DAILY",
        data_date,
        db_path,
        stock_codes,
        domains,
        active_only=stock_codes is None,
        client=client,
        api=api,
        financial_fields=financial_fields,
    )


def repair_stock(
    stock_code: str,
    start_date: str,
    end_date: str,
    domains: Sequence[str] = ALL_DOMAINS,
    db_path: str | Path = DEFAULT_DB_PATH,
    client: TdxClient | None = None,
    api: Any = None,
    financial_fields: Sequence[str] = CORE_FN_FIELDS,
) -> dict[str, Any]:
    # 定向重新获取一只股票指定日期和数据域的数据。
    return _run_update(
        "STOCK_DATA_REPAIR",
        end_date,
        db_path,
        [stock_code],
        domains,
        start_date=start_date,
        client=client,
        api=api,
        financial_fields=financial_fields,
    )


def check_data(
    db_path: str | Path = DEFAULT_DB_PATH,
    stock_codes: Sequence[str] | None = None,
    check_profile: str = "full",
    data_date: str | date | None = None,
) -> dict[str, Any]:
    # 按每日、快速或完整模式执行数据库质量检查。
    if check_profile not in {"daily", "quick", "full"}:
        raise ValueError(f"不支持的检查模式: {check_profile}")
    connection = db.connect(db_path)
    try:
        db.init_db(connection)
        if check_profile == "full":
            result: dict[str, Any] = {
                "check_profile": check_profile,
                "table_counts": db.table_counts(connection, stock_codes),
                "checks": db.database_checks(connection),
            }
        else:
            task_date = normalize_date(data_date)
            checks = db.daily_database_checks(connection, task_date)
            if check_profile == "quick":
                checks["quick_check"] = db.quick_database_check(connection)
            result = {
                "check_profile": check_profile,
                "data_date": task_date,
                "table_counts": db.daily_table_counts(connection, task_date),
                "checks": checks,
            }
        if stock_codes and check_profile == "full":
            result["stock_counts"] = db.stock_table_counts(
                connection,
                stock_codes,
            )
        return result
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    # 创建股票详细数据命令行参数解析器。
    parser = argparse.ArgumentParser(description="通达信股票详细历史数据获取")
    parser.add_argument(
        "command",
        choices=(
            "init-db",
            "full-load",
            "bulk-full-load",
            "daily-update",
            "repair-stock",
            "check-data",
        ),
    )
    parser.add_argument("--db", dest="db_path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--date", dest="data_date")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--stocks", help="逗号分隔的标准股票代码")
    parser.add_argument("--domains", help="bar,capital,action,trade,financial")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--bar-batch-size", type=int, default=3)
    parser.add_argument("--metric-batch-size", type=int, default=10)
    parser.add_argument(
        "--financial-profile",
        choices=("core", "all"),
        default="core",
    )
    parser.add_argument(
        "--check-profile",
        choices=("daily", "quick", "full"),
        default="daily",
        help="check-data 检查强度；每日任务默认只检查当天增量",
    )
    return parser


def main() -> None:
    # 解析命令行并执行股票详细数据任务。
    configure_logging()
    args = _build_parser().parse_args()
    stock_codes = parse_stock_codes(args.stocks)
    if args.command == "init-db":
        result = init_database(args.db_path)
    elif args.command == "full-load":
        result = full_load(
            args.end_date or args.data_date,
            args.db_path,
            stock_codes,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            financial_fields=(
                CORE_FN_FIELDS if args.financial_profile == "core" else FN_FIELDS
            ),
        )
    elif args.command == "bulk-full-load":
        result = bulk_full_load(
            args.end_date or args.data_date,
            args.db_path,
            stock_codes,
            args.bar_batch_size,
            args.metric_batch_size,
            args.start_date,
            parse_domains(args.domains),
            CORE_FN_FIELDS if args.financial_profile == "core" else FN_FIELDS,
        )
    elif args.command == "daily-update":
        result = daily_update(
            args.data_date,
            args.db_path,
            stock_codes,
            parse_domains(args.domains),
            financial_fields=(
                CORE_FN_FIELDS if args.financial_profile == "core" else FN_FIELDS
            ),
        )
    elif args.command == "repair-stock":
        if not stock_codes or len(stock_codes) != 1:
            raise ValueError("repair-stock 必须通过 --stocks 指定一只股票")
        if not args.start_date or not args.end_date:
            raise ValueError("repair-stock 必须指定 --start-date 和 --end-date")
        result = repair_stock(
            stock_codes[0],
            args.start_date,
            args.end_date,
            parse_domains(args.domains),
            args.db_path,
            financial_fields=(
                CORE_FN_FIELDS if args.financial_profile == "core" else FN_FIELDS
            ),
        )
    else:
        result = check_data(
            args.db_path,
            stock_codes,
            args.check_profile,
            args.data_date,
        )
    print(result)


if __name__ == "__main__":
    main()
