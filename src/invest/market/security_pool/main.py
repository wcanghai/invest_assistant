"""证券池首次全量、每日更新和命令行入口。"""

from __future__ import annotations

from invest.core.settings import ROOT as PROJECT_HOME
from invest.core.settings import load_settings

import argparse
import logging
from datetime import date
from pathlib import Path
from typing import Any

from invest.providers.tdx_client import TdxClient

from invest.storage import security_pool as db
from invest.providers.security_pool import fetch_classifications
from invest.providers.security_pool import fetch_convertible_bonds
from invest.providers.security_pool import fetch_etfs
from invest.providers.security_pool import fetch_ipo_info
from invest.providers.security_pool import fetch_stock_bundle
from invest.providers.security_pool import init_tdx
from invest.market.security_pool.processor import build_convertible_bond_rows
from invest.market.security_pool.processor import build_current_row
from invest.market.security_pool.processor import build_etf_rows
from invest.market.security_pool.processor import build_inactive_row
from invest.market.security_pool.processor import build_ipo_rows
from invest.market.security_pool.processor import build_master_row
from invest.market.security_pool.processor import build_sector_rows
from invest.market.security_pool.processor import validate_daily_rows


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = PROJECT_HOME
DEFAULT_DB_PATH = load_settings().databases["market"]


def configure_logging(level: int = logging.INFO) -> None:
    # 配置适合命令行运行的基础日志格式。
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def normalize_data_date(value: str | date | None) -> str:
    # 将可选任务日期转换为 YYYY-MM-DD。
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()


def init_database(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    # 创建证券池 SQLite 数据库并返回各表记录数。
    connection = db.connect(db_path)
    try:
        db.init_db(connection)
        return db.table_counts(connection)
    finally:
        connection.close()


def _resolve_client(
    client: TdxClient | None,
    api: Any,
) -> TdxClient:
    # 使用已提供客户端或创建通达信客户端。
    return client if client is not None else init_tdx(api=api, script_file=__file__)


def _fetch_other_data(
    client: TdxClient,
    data_date: str,
) -> tuple[list[dict] | None, list[dict] | None, list[dict] | None]:
    # 获取并转换 IPO、可转债和 ETF 独立数据。
    raw_ipo = fetch_ipo_info(client)
    raw_bonds = fetch_convertible_bonds(client)
    raw_etfs = fetch_etfs(client)
    ipo_rows = build_ipo_rows(raw_ipo) if raw_ipo is not None else None
    bond_rows = (
        build_convertible_bond_rows(raw_bonds, data_date)
        if raw_bonds is not None
        else None
    )
    etf_rows = build_etf_rows(raw_etfs, data_date) if raw_etfs is not None else None
    return ipo_rows, bond_rows, etf_rows


def _collect_stock_rows(
    client: TdxClient,
    data_date: str,
    classifications: dict[str, dict[str, str] | None],
    master_codes: set[str],
    previous_rows: dict[str, dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    # 获取全部活动股票并构建主表、当前宽表和板块关系记录。
    current_members = classifications["5"] or {}
    current_codes = set(current_members)
    all_seen_codes = master_codes | current_codes
    master_rows: list[dict] = []
    current_rows: list[dict] = []
    sector_rows: list[dict] = []

    for index, stock_code in enumerate(sorted(all_seen_codes), start=1):
        previous_row = previous_rows.get(stock_code)
        if stock_code not in current_codes and previous_row is not None:
            current_rows.append(build_inactive_row(previous_row, data_date))
            continue

        bundle = fetch_stock_bundle(client, stock_code, include_relations=True)
        stock_info = bundle["stock_info"]
        daily_info = bundle["daily_info"]
        if stock_code not in master_codes:
            master_rows.append(build_master_row(stock_code, stock_info, data_date))
        current_rows.append(
            build_current_row(
                stock_code,
                data_date,
                classifications,
                stock_info,
                daily_info,
                previous_row,
            )
        )
        sector_rows.extend(
            build_sector_rows(stock_code, bundle["relations"], data_date)
        )
        if index % 100 == 0 or index == len(all_seen_codes):
            LOGGER.info("股票数据获取进度 %s/%s", index, len(all_seen_codes))
    return master_rows, current_rows, sector_rows


def _run_update(
    task_type: str,
    data_date: str | date | None,
    db_path: str | Path,
    client: TdxClient | None = None,
    api: Any = None,
) -> dict[str, int]:
    # 执行全量或每日证券池获取、校验和事务写入。
    task_date = normalize_data_date(data_date)
    connection = db.connect(db_path)
    db.init_db(connection)
    log_id = db.start_log(connection, task_date, task_type)
    try:
        active_client = _resolve_client(client, api)
        previous_rows = db.load_current_rows(connection)
        master_codes = db.load_master_codes(connection)
        previous_active_count = sum(
            row.get("is_active") == 1 for row in previous_rows.values()
        )

        classifications = fetch_classifications(active_client)
        master_rows, current_rows, sector_rows = _collect_stock_rows(
            active_client,
            task_date,
            classifications,
            master_codes,
            previous_rows,
        )
        expected_codes = master_codes | set(classifications["5"] or {})
        errors = validate_daily_rows(
            current_rows,
            expected_codes,
            previous_active_count,
        )
        if errors:
            raise RuntimeError("；".join(errors[:20]))

        ipo_rows, bond_rows, etf_rows = _fetch_other_data(active_client, task_date)
        db.save_daily_data(
            connection,
            task_date,
            master_rows,
            current_rows,
            sector_rows,
            ipo_rows,
            bond_rows,
            etf_rows,
            log_id,
        )
        counts = db.table_counts(connection)
        LOGGER.info("证券池任务成功: %s", counts)
        return counts
    except Exception as exc:
        connection.rollback()
        db.finish_log(
            connection,
            log_id,
            "FAILED",
            error_message=str(exc),
        )
        LOGGER.exception("证券池任务失败")
        raise
    finally:
        connection.close()


def full_load(
    data_date: str | date | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    client: TdxClient | None = None,
    api: Any = None,
) -> dict[str, int]:
    # 执行首次全量证券池数据获取和写入。
    return _run_update("FULL", data_date, db_path, client=client, api=api)


def daily_update(
    data_date: str | date | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    client: TdxClient | None = None,
    api: Any = None,
) -> dict[str, int]:
    # 执行交易日收盘后的每日证券池更新和快照归档。
    return _run_update("DAILY", data_date, db_path, client=client, api=api)


def update_ipo_only(
    data_date: str | date | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    client: TdxClient | None = None,
    api: Any = None,
) -> dict[str, int]:
    # 非交易日或盘前只更新新股新债申购信息。
    task_date = normalize_data_date(data_date)
    connection = db.connect(db_path)
    db.init_db(connection)
    log_id = db.start_log(connection, task_date, "IPO")
    try:
        active_client = _resolve_client(client, api)
        raw_records = fetch_ipo_info(active_client)
        if raw_records is None:
            raise RuntimeError("新股新债申购信息获取失败")
        rows = build_ipo_rows(raw_records)
        with connection:
            db.upsert_ipo_info(connection, rows)
            db.finish_log(
                connection,
                log_id,
                "SUCCESS",
                stock_count=len(rows),
                commit=False,
            )
        return db.table_counts(connection)
    except Exception as exc:
        connection.rollback()
        db.finish_log(
            connection,
            log_id,
            "FAILED",
            error_message=str(exc),
        )
        raise
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    # 创建证券池命令行参数解析器。
    parser = argparse.ArgumentParser(description="通达信股票证券池数据获取")
    parser.add_argument(
        "command",
        choices=("init-db", "full-load", "daily-update", "update-ipo"),
    )
    parser.add_argument("--date", dest="data_date")
    parser.add_argument("--db", dest="db_path", default=str(DEFAULT_DB_PATH))
    return parser


def main() -> None:
    # 解析命令行并执行对应证券池任务。
    configure_logging()
    args = _build_parser().parse_args()
    if args.command == "init-db":
        result = init_database(args.db_path)
    elif args.command == "full-load":
        result = full_load(args.data_date, args.db_path)
    elif args.command == "daily-update":
        result = daily_update(args.data_date, args.db_path)
    else:
        result = update_ipo_only(args.data_date, args.db_path)
    print(result)


if __name__ == "__main__":
    main()
