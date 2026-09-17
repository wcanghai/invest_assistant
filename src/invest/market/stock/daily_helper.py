"""每日刷新脚本使用的交易日和证券范围辅助命令。"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

from invest.providers.security_pool import init_tdx


def load_scope_codes(db_path: str | Path, scope: str) -> list[str]:
    # 从证券当前表读取中证500或全部活动A股代码。
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        sql = (
            "SELECT stock_code FROM stock_current "
            "WHERE is_active=1 AND is_all_a=1"
        )
        if scope == "Csi500":
            sql += " AND is_zz500=1"
        sql += " ORDER BY stock_code"
        return [row[0] for row in connection.execute(sql)]
    finally:
        connection.close()


def is_trading_day(data_date: str) -> bool:
    # 通过通达信交易日历判断指定日期是否为A股交易日。
    normalized = date.fromisoformat(data_date).strftime("%Y%m%d")
    client = init_tdx(script_file=__file__)
    dates = client.get_trading_dates("SH", normalized, normalized, -1) or []
    return normalized in dates


def _build_parser() -> argparse.ArgumentParser:
    # 创建每日刷新辅助命令的参数解析器。
    parser = argparse.ArgumentParser(description="每日股票刷新辅助命令")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scope_parser = subparsers.add_parser("scope")
    scope_parser.add_argument("--db", required=True)
    scope_parser.add_argument("--scope", choices=("Csi500", "AllA"), required=True)

    calendar_parser = subparsers.add_parser("is-trading-day")
    calendar_parser.add_argument("--date", required=True)
    return parser


def main() -> None:
    # 执行证券范围查询或交易日判断并输出机器可读结果。
    args = _build_parser().parse_args()
    if args.command == "scope":
        print(",".join(load_scope_codes(args.db, args.scope)))
    else:
        print(f"TRADE_DAY={is_trading_day(args.date)}")


if __name__ == "__main__":
    main()
