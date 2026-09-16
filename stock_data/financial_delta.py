"""Two-stage incremental financial report synchronization."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import db
from .fetcher import CORE_FN_FIELDS, fetch_financial_reports, init_tdx
from .processor import build_financial_rows, validate_financial_rows


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "security_pool.db"
# These balance-sheet/income fields are broadly populated across industries.
PROBE_FIELDS = ("FN1", "FN40", "FN159")


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("批次大小必须大于零")
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


def sync_financial_delta(
    start_date: str,
    end_date: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    probe_batch_size: int = 100,
    detail_batch_size: int = 5,
    client: Any = None,
) -> dict[str, Any]:
    """Probe cheaply for reports, then fetch full core fields only for hits."""
    start = date.fromisoformat(start_date).isoformat()
    end = date.fromisoformat(end_date).isoformat()
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")

    connection = db.connect(db_path)
    db.init_db(connection)
    log_id = db.start_log(connection, end, "STOCK_FINANCIAL_DELTA")
    errors: dict[str, str] = {}
    scanned = 0
    hit_codes: set[str] = set()
    written_rows = 0
    try:
        codes = [
            str(row[0])
            for row in connection.execute(
                "SELECT current.stock_code FROM stock_current AS current "
                "LEFT JOIN stock_data_sync_state AS state "
                "ON state.stock_code=current.stock_code "
                "AND state.data_domain='financial' "
                "WHERE current.is_active=1 AND current.is_all_a=1 "
                "AND COALESCE(state.last_success_date,'')<? "
                "ORDER BY current.stock_code",
                (end,),
            )
        ]
        active_client = client or init_tdx(script_file=__file__)
        probe_batches = _chunks(codes, probe_batch_size)
        for index, batch in enumerate(probe_batches, start=1):
            try:
                raw = fetch_financial_reports(
                    active_client,
                    batch,
                    start,
                    end,
                    fields=PROBE_FIELDS,
                )
                rows = build_financial_rows(raw)
                batch_hits = {str(row["stock_code"]) for row in rows}
                hit_codes.update(batch_hits)
                no_hits = [code for code in batch if code not in batch_hits]
                with connection:
                    db.save_sync_state(connection, no_hits, ("financial",), end)
                scanned += len(batch)
            except Exception as exc:
                LOGGER.exception("财务公告筛查批次 %s 失败", index)
                for code in batch:
                    errors[code] = f"PROBE: {exc}"
            if index % 10 == 0 or index == len(probe_batches):
                LOGGER.info(
                    "财务公告筛查 %s/%s，已扫描 %s，命中 %s",
                    index,
                    len(probe_batches),
                    scanned,
                    len(hit_codes),
                )

        detail_batches = _chunks(sorted(hit_codes), detail_batch_size)
        for index, batch in enumerate(detail_batches, start=1):
            try:
                raw = fetch_financial_reports(
                    active_client,
                    batch,
                    start,
                    end,
                    fields=CORE_FN_FIELDS,
                )
                rows = build_financial_rows(raw)
                validation_errors = validate_financial_rows(rows)
                if validation_errors:
                    raise RuntimeError("；".join(validation_errors[:20]))
                with connection:
                    written_rows += db.upsert_financial_rows(connection, rows)
                    db.save_sync_state(connection, batch, ("financial",), end)
            except Exception as exc:
                LOGGER.exception("财务核心字段批次 %s 失败", index)
                for code in batch:
                    errors[code] = f"DETAIL: {exc}"
            if index % 10 == 0 or index == len(detail_batches):
                LOGGER.info(
                    "财务核心字段 %s/%s，累计写入 %s",
                    index,
                    len(detail_batches),
                    written_rows,
                )

        succeeded = scanned - len(errors)
        status = "SUCCESS" if not errors else "PARTIAL_SUCCESS"
        db.finish_log(
            connection,
            log_id,
            status,
            stock_count=max(succeeded, 0),
            error_message=(json.dumps(errors, ensure_ascii=False)[:4000] if errors else None),
        )
        return {
            "status": status,
            "requested_stocks": len(codes),
            "scanned_stocks": scanned,
            "report_stock_count": len(hit_codes),
            "written_rows": written_rows,
            "errors": errors,
        }
    except KeyboardInterrupt:
        connection.rollback()
        db.finish_log(
            connection,
            log_id,
            "INTERRUPTED",
            stock_count=scanned,
            error_message="用户或调度器中断任务",
        )
        raise
    except Exception as exc:
        connection.rollback()
        db.finish_log(
            connection,
            log_id,
            "FAILED",
            stock_count=scanned,
            error_message=str(exc)[:4000],
        )
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="两阶段核心财务增量同步")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--probe-batch-size", type=int, default=100)
    parser.add_argument("--detail-batch-size", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    print(sync_financial_delta(
        args.start_date,
        args.end_date,
        args.db,
        args.probe_batch_size,
        args.detail_batch_size,
    ))


if __name__ == "__main__":
    main()
