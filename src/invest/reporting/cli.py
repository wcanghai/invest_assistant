"""采集、生成和导出日报的命令入口。"""

import argparse
import json
from pathlib import Path

from invest.reporting.common import CONFIG
from invest.reporting.common import REPORT_DB
from invest.reporting.common import ROOT
from invest.reporting.common import SOURCE_DB
from invest.reporting.common import now_iso
from invest.reporting.service import generate
from invest.storage.reports import connect
from invest.storage.reports import get_report
from invest.storage.reports import list_reports


def main():
    # CLI 与网页共用同一生成流程，历史补报仅使用本地已存档网页数据。
    parser = argparse.ArgumentParser(description="每日市场观察")
    commands = ("collect", "generate", "refresh", "ensure", "list", "export")
    parser.add_argument("command", choices=commands)
    parser.add_argument("--date", help="日报自然日，默认今天；A 股使用截至该日最近行情")
    parser.add_argument("--source-db", type=Path, default=SOURCE_DB)
    parser.add_argument("--report-db", type=Path, default=REPORT_DB)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--id", type=int, help="导出指定报告版本")
    parser.add_argument("--output", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    should_collect = args.command in ("collect", "refresh")
    if args.command == "ensure":
        conn = connect(args.report_db)
        try:
            exists = conn.execute("SELECT 1 FROM reports WHERE report_date=? LIMIT 1",
                                  (args.date or now_iso()[:10],)).fetchone()
            should_collect = not exists
        finally:
            conn.close()
    if should_collect:
        from invest.providers.web import collect

        result = collect(args.config, args.report_db)
        print(json.dumps(result, ensure_ascii=False))
    if args.command in ("generate", "refresh", "ensure"):
        report = generate(args.date, args.source_db, args.report_db, args.config)
        args.output.mkdir(parents=True, exist_ok=True)
        destination = args.output / f"market_report_{report['report_date']}_v{report['version']}.md"
        destination.write_text(report["markdown"], encoding="utf-8")
        print(json.dumps({"id": report["id"], "date": report["report_date"],
                          "version": report["version"], "status": report["status"],
                          "file": str(destination)}, ensure_ascii=False))
    if args.command in ("list", "export"):
        conn = connect(args.report_db)
        try:
            if args.command == "list":
                print(json.dumps(list_reports(conn), ensure_ascii=False))
            else:
                report = get_report(conn, args.id)
                if report is None:
                    raise SystemExit("尚无报告")
                args.output.mkdir(parents=True, exist_ok=True)
                name = f"market_report_{report['report_date']}_v{report['version']}.md"
                (args.output / name).write_text(report["markdown"], encoding="utf-8")
                print(args.output / name)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
