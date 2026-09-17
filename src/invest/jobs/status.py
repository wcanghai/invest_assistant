"""Check whether daily A-share and ETF data have reached a target date."""

from __future__ import annotations

from invest.core.settings import load_settings

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path


DEFAULT_DB = load_settings().databases["market"]
STOCK_DOMAINS = ("bar", "capital", "action", "trade")


def get_status(database: Path, target_date: str) -> dict[str, object]:
    # 只读检查同步状态，错误路径不得创建新的空数据库。
    date.fromisoformat(target_date)
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        current_date = connection.execute(
            "SELECT MAX(data_date) FROM stock_current"
        ).fetchone()[0]
        expected_stocks = 0
        completed_stocks = 0
        missing_stocks: list[str] = []
        if current_date:
            expected_stocks = connection.execute(
                """
                SELECT COUNT(*)
                FROM stock_current
                WHERE data_date = ? AND is_active = 1 AND is_all_a = 1
                """,
                (current_date,),
            ).fetchone()[0]
            placeholders = ",".join("?" for _ in STOCK_DOMAINS)
            completed_stocks = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT current.stock_code
                    FROM stock_current AS current
                    JOIN stock_data_sync_state AS state
                      ON state.stock_code = current.stock_code
                    WHERE current.data_date = ?
                      AND current.is_active = 1
                      AND current.is_all_a = 1
                      AND state.data_domain IN ({placeholders})
                      AND state.last_success_date >= ?
                    GROUP BY current.stock_code
                    HAVING COUNT(DISTINCT state.data_domain) = ?
                )
                """,
                (current_date, *STOCK_DOMAINS, target_date, len(STOCK_DOMAINS)),
            ).fetchone()[0]
            if current_date >= target_date and completed_stocks < expected_stocks:
                placeholders = ",".join("?" for _ in STOCK_DOMAINS)
                missing_stocks = [
                    row[0]
                    for row in connection.execute(
                        f"""
                        SELECT current.stock_code
                        FROM stock_current AS current
                        LEFT JOIN stock_data_sync_state AS state
                          ON state.stock_code = current.stock_code
                         AND state.data_domain IN ({placeholders})
                         AND state.last_success_date >= ?
                        WHERE current.data_date = ?
                          AND current.is_active = 1
                          AND current.is_all_a = 1
                        GROUP BY current.stock_code
                        HAVING COUNT(DISTINCT state.data_domain) < ?
                        ORDER BY current.stock_code
                        """,
                        (*STOCK_DOMAINS, target_date, current_date, len(STOCK_DOMAINS)),
                    )
                ]

        expected_etfs = connection.execute(
            "SELECT COUNT(*) FROM etf_master"
        ).fetchone()[0]
        completed_etfs = connection.execute(
            """
            SELECT COUNT(*)
            FROM etf_master AS master
            JOIN etf_data_sync_state AS state
              ON state.etf_code = master.etf_code
             AND state.data_domain = 'daily'
            WHERE state.last_success_date >= ?
            """,
            (target_date,),
        ).fetchone()[0]

        stock_complete = bool(
            current_date
            and current_date >= target_date
            and expected_stocks > 0
            and completed_stocks == expected_stocks
        )
        etf_complete = bool(
            expected_etfs > 0 and completed_etfs == expected_etfs
        )
        return {
            "target_date": target_date,
            "complete": stock_complete and etf_complete,
            "stock": {
                "complete": stock_complete,
                "current_date": current_date,
                "expected": expected_stocks,
                "completed": completed_stocks,
                "missing_codes": missing_stocks,
            },
            "etf": {
                "complete": etf_complete,
                "expected": expected_etfs,
                "completed": completed_etfs,
            },
        }
    finally:
        connection.close()


def main() -> None:
    # 输出同步完整性状态，并用退出码区分完成与待补采。
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()
    status = get_status(args.db, args.date)
    print(json.dumps(status, ensure_ascii=False))
    raise SystemExit(0 if status["complete"] else 10)


if __name__ == "__main__":
    main()
