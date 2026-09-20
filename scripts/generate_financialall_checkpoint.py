"""Seed the FinancialAll checkpoint from rows already containing every FN field."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from invest.providers.stock import FN_FIELDS


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate completed full-financial codes")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-date", default="2004-01-01")
    args = parser.parse_args()

    uri = f"file:{args.db.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        latest_report = connection.execute(
            "SELECT MAX(report_date) FROM stock_financial_report"
        ).fetchone()[0]
        if not latest_report:
            completed: list[str] = []
        else:
            candidates: set[str] = set()
            for stock_code, raw_values in connection.execute(
                "SELECT stock_code,raw_values_json FROM stock_financial_report "
                "WHERE report_date=?",
                (latest_report,),
            ):
                values = json.loads(raw_values or "{}")
                if all(field in values for field in FN_FIELDS):
                    candidates.add(str(stock_code))

            incomplete: set[str] = set()
            placeholders = ",".join("?" for _ in candidates)
            if candidates:
                sql = (
                    "SELECT stock_code,raw_values_json FROM stock_financial_report "
                    f"WHERE report_date>=? AND stock_code IN ({placeholders})"
                )
                parameters = [args.start_date, *sorted(candidates)]
                for stock_code, raw_values in connection.execute(sql, parameters):
                    values = json.loads(raw_values or "{}")
                    if not all(field in values for field in FN_FIELDS):
                        incomplete.add(str(stock_code))
            completed = sorted(candidates - incomplete)
    finally:
        connection.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(f"{stock_code}\n" for stock_code in completed),
        encoding="ascii",
    )
    print(
        json.dumps(
            {
                "latest_report": latest_report,
                "required_field_count": len(FN_FIELDS),
                "completed_stocks": len(completed),
                "checkpoint": str(args.output.resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
