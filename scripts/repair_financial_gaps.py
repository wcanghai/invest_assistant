"""Audit and repair missing core financial reports for the active A-share pool."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from invest.market.stock.main import bulk_full_load
from invest.providers.stock import CORE_FN_FIELDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "databases" / "market.db"


def _due_report_dates(as_of: date, start_year: int = 2004) -> list[str]:
    """Return standard report periods whose statutory publication window has ended."""
    periods: list[str] = []
    for year in range(start_year, as_of.year + 1):
        candidates = (
            (date(year, 3, 31), date(year, 4, 30)),
            (date(year, 6, 30), date(year, 8, 31)),
            (date(year, 9, 30), date(year, 10, 31)),
            (date(year, 12, 31), date(year + 1, 4, 30)),
        )
        periods.extend(period.isoformat() for period, due in candidates if due <= as_of)
    return periods


def audit_financial_gaps(db_path: Path, as_of: date) -> dict[str, Any]:
    """Find missing due periods and securities for which TDX returned no reports."""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        universe = {
            row["stock_code"]: dict(row)
            for row in connection.execute(
                "SELECT master.stock_code,current.stock_name,master.list_date,"
                "current.is_tradable FROM stock_master AS master "
                "JOIN stock_current AS current USING(stock_code) "
                "WHERE current.is_active=1 AND current.is_all_a=1"
            )
        }
        reports: dict[str, set[str]] = defaultdict(set)
        for stock_code, report_date in connection.execute(
            "SELECT stock_code,report_date FROM stock_financial_report "
            "WHERE report_date>=?",
            ("2004-01-01",),
        ):
            reports[str(stock_code)].add(str(report_date))

        due_periods = _due_report_dates(as_of)
        missing_periods: list[dict[str, Any]] = []
        for stock_code, stock in universe.items():
            list_date = stock["list_date"]
            if not list_date:
                continue
            expected = [
                period
                for period in due_periods
                if period >= max("2004-01-01", str(list_date))
            ]
            missing = [
                period for period in expected if period not in reports.get(stock_code, set())
            ]
            if missing:
                missing_periods.append(
                    {
                        "stock_code": stock_code,
                        "stock_name": stock["stock_name"],
                        "list_date": list_date,
                        "missing_periods": missing,
                    }
                )

        no_report = [
            {
                "stock_code": stock_code,
                "stock_name": stock["stock_name"],
                "list_date": stock["list_date"],
                "is_tradable": stock["is_tradable"],
            }
            for stock_code, stock in universe.items()
            if stock_code not in reports
        ]
        target_codes = sorted(
            {row["stock_code"] for row in missing_periods}
            | {row["stock_code"] for row in no_report}
        )

        latest_due = due_periods[-1] if due_periods else None
        key_distribution: dict[int, int] = defaultdict(int)
        latest_stocks = 0
        if latest_due:
            for (raw_values,) in connection.execute(
                "SELECT raw_values_json FROM stock_financial_report WHERE report_date=?",
                (latest_due,),
            ):
                latest_stocks += 1
                try:
                    key_distribution[len(json.loads(raw_values or "{}"))] += 1
                except (TypeError, ValueError, json.JSONDecodeError):
                    key_distribution[-1] += 1

        return {
            "as_of": as_of.isoformat(),
            "active_all_a": len(universe),
            "stocks_with_financial_reports": sum(
                stock_code in reports for stock_code in universe
            ),
            "latest_due_report_date": latest_due,
            "latest_due_report_stocks": latest_stocks,
            "latest_key_count_distribution": dict(sorted(key_distribution.items())),
            "missing_period_count": sum(
                len(row["missing_periods"]) for row in missing_periods
            ),
            "missing_period_stocks": missing_periods,
            "no_report_stocks": no_report,
            "target_codes": target_codes,
        }
    finally:
        connection.close()


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return lines


def write_report(
    output: Path,
    before: dict[str, Any],
    after: dict[str, Any],
    fetch_result: dict[str, Any] | None,
) -> None:
    """Write a compact, human-readable repair report."""
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 财务核心数据缺口补采报告",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "## 执行摘要",
        "",
        f"- A股证券数：{after['active_all_a']}",
        f"- 补采目标股票数：{len(before['target_codes'])}",
        f"- 补采前缺少报告期：{before['missing_period_count']}",
        f"- 补采后缺少报告期：{after['missing_period_count']}",
        f"- 补采前完全无财报股票：{len(before['no_report_stocks'])}",
        f"- 补采后完全无财报股票：{len(after['no_report_stocks'])}",
        f"- 最近应披露报告期：{after['latest_due_report_date']}",
        f"- 最近报告期覆盖股票：{after['latest_due_report_stocks']}",
        "",
    ]
    if fetch_result is not None:
        lines.extend(
            [
                "## 采集任务结果",
                "",
                "```json",
                json.dumps(fetch_result, ensure_ascii=False, indent=2, default=str),
                "```",
                "",
            ]
        )

    lines.extend(["## 补采后仍缺少的报告期", ""])
    gap_rows = [
        [
            row["stock_code"],
            row["stock_name"] or "",
            row["list_date"] or "",
            "、".join(row["missing_periods"]),
        ]
        for row in after["missing_period_stocks"]
    ]
    lines.extend(
        _markdown_table(["股票代码", "名称", "上市日期", "缺少报告期"], gap_rows)
        if gap_rows
        else ["无。"]
    )

    lines.extend(["", "## 补采后仍无财报记录的证券", ""])
    no_report_rows = [
        [
            row["stock_code"],
            row["stock_name"] or "",
            row["list_date"] or "",
            row["is_tradable"],
        ]
        for row in after["no_report_stocks"]
    ]
    lines.extend(
        _markdown_table(["股票代码", "名称", "上市日期", "可交易"], no_report_rows)
        if no_report_rows
        else ["无。"]
    )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 本任务只获取92个核心财务字段，不执行438字段全量归档。",
            "- 若补采后仍无数据，通常表示通达信当前未提供该证券或报告期的数据。",
            "- 无上市日期的待上市或新上市证券保留在报告中，后续可再次执行本脚本探测。",
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="补采A股核心财务数据缺口")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start-date", default="2004-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--metric-batch-size", type=int, default=10)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    as_of = date.fromisoformat(args.end_date)
    before = audit_financial_gaps(args.db, as_of)
    print(
        json.dumps(
            {
                "phase": "before",
                "target_count": len(before["target_codes"]),
                "missing_period_count": before["missing_period_count"],
                "no_report_count": len(before["no_report_stocks"]),
                "target_codes": before["target_codes"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )

    fetch_result: dict[str, Any] | None = None
    if not args.audit_only and before["target_codes"]:
        fetch_result = bulk_full_load(
            end_date=args.end_date,
            db_path=args.db,
            stock_codes=before["target_codes"],
            metric_batch_size=args.metric_batch_size,
            start_date=args.start_date,
            domains=("financial",),
            financial_fields=CORE_FN_FIELDS,
        )

    after = audit_financial_gaps(args.db, as_of)
    output = args.output or (
        PROJECT_ROOT
        / "reports"
        / "audit"
        / f"financial_gap_repair_{datetime.now():%Y%m%d_%H%M%S}.md"
    )
    write_report(output, before, after, fetch_result)
    print(
        json.dumps(
            {
                "phase": "after",
                "missing_period_count": after["missing_period_count"],
                "no_report_count": len(after["no_report_stocks"]),
                "report": str(output.resolve()),
                "fetch_result": fetch_result,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
