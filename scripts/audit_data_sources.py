"""Audit local coverage and probe public data sources used by research planning."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


USER_AGENT = "invest-data-source-audit/1.0"


def request(url: str, method: str = "GET", data: dict[str, str] | None = None) -> dict:
    """Request a public endpoint and return status, type, size, and a short sample."""
    payload = None
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        payload = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
    try:
        req = urllib.request.Request(url, data=payload, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as response:
            body = response.read()
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "bytes": len(body),
                "sample": body[:240].decode("utf-8", errors="replace"),
            }
    except Exception as exc:  # pragma: no cover - network failures vary by host
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def local_audit(database: Path) -> dict[str, object]:
    """Read local table counts and coverage without opening the database for writing."""
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    queries = {
        "latest_dates": """
            SELECT (SELECT MAX(trade_date) FROM stock_daily_bar),
                   (SELECT MAX(trade_date) FROM etf_daily),
                   (SELECT MAX(report_date) FROM stock_financial_report),
                   (SELECT MAX(snapshot_date) FROM stock_snapshot)
        """,
        "etf_master_coverage": """
            SELECT COUNT(*), SUM(list_date IS NOT NULL),
                   SUM(underlying_code IS NOT NULL AND underlying_code <> '')
            FROM etf_master
        """,
        "etf_latest_fields": """
            SELECT COUNT(*), SUM(unit_nav IS NOT NULL), SUM(size_10k IS NOT NULL),
                   SUM(premium_discount_pct IS NOT NULL), SUM(net_subscription_10k IS NOT NULL)
            FROM etf_daily WHERE trade_date = (SELECT MAX(trade_date) FROM etf_daily)
        """,
        "stock_current_fields": """
            SELECT COUNT(*), SUM(industry_name IS NOT NULL AND industry_name <> ''),
                   SUM(pe_ttm IS NOT NULL), SUM(pb_mrq IS NOT NULL),
                   SUM(dividend_yield IS NOT NULL)
            FROM stock_current WHERE is_active = 1
        """,
        "sector_history": """
            SELECT MIN(snapshot_date), MAX(snapshot_date), COUNT(DISTINCT snapshot_date),
                   COUNT(DISTINCT sector_name)
            FROM stock_sector_snapshot
        """,
        "etf_classification": """
            SELECT COUNT(*), COUNT(DISTINCT etf_code), SUM(verified = 1),
                   SUM(historical_verified = 1) FROM etf_classification
        """,
        "etf_actions": """
            SELECT COUNT(*), COUNT(DISTINCT etf_code), SUM(verified = 1) FROM etf_action
        """,
    }
    result = {name: connection.execute(sql).fetchone() for name, sql in queries.items()}
    connection.close()
    return result


def probes() -> dict[str, dict]:
    """Probe representative official and public endpoints used by the source plan."""
    results = {
        "cninfo_security_map": request(
            "https://www.cninfo.com.cn/new/data/szse_stock.json"
        ),
        "cninfo_announcement_query": request(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            method="POST",
            data={
                "pageNum": "1",
                "pageSize": "30",
                "column": "szse",
                "tabName": "fulltext",
                "plate": "sz",
                "stock": "000001,gssz0000001",
                "seDate": "2026-09-01~2026-09-20",
                "isHLtitle": "true",
            },
        ),
        "cninfo_pdf": request(
            "https://static.cninfo.com.cn/finalpage/2026-06-05/1225351323.PDF"
        ),
        "sse_etf_page": request("https://www.sse.com.cn/assortment/fund/etf/disclosure/overview/"),
        "sse_etf_list_page": request("https://etf.sse.com.cn/"),
        "szse_etf_page": request("https://fund.szse.cn/marketdata/etf/"),
        "szse_fund_notice": request("https://www.szse.cn/disclosure/notice/fund/index.html"),
        "szse_etf_size": request(
            "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&"
            "CATALOGID=scsj_fund_jjgm&TABKEY=tab1&jjlb=ETF&txtDm=159915&"
            "txtStart=2026-09-01&txtEnd=2026-09-18&PAGENO=1"
        ),
        "csindex_constituents": request(
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
            "file/autofile/cons/000300cons.xls"
        ),
        "sina_company_events": request(
            "https://money.finance.sina.com.cn/corp/view/"
            "vCB_AllMemordDetail.php?stockid=000001"
        ),
        "fund_profile": request("https://fundf10.eastmoney.com/jbgk_510300.html"),
        "pbc_statistics": request("https://www.pbc.gov.cn/diaochatongjisi/116219/index.html"),
        "stats_bureau": request("https://data.stats.gov.cn/easyquery.htm?cn=A01"),
    }
    return results


def markdown(audit: dict[str, object]) -> str:
    """Render a compact, reproducible audit report."""
    lines = [
        "# 数据源可行性审计",
        "",
        f"审计时间：{audit['generated_at']}",
        "",
        "## 本地覆盖快照",
        "",
        "```json",
        json.dumps(audit["local"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 公开来源探测",
        "",
        "状态为 HTTP 200 仅表示入口可访问；结构化字段、分页、历史完整性仍需按来源适配器单独验证。",
        "",
        "| 来源 | 状态 | 类型 | 字节数 | 结论 |",
        "|---|---:|---|---:|---|",
    ]
    conclusions = {
        "cninfo_security_map": "可作为证券映射入口",
        "cninfo_announcement_query": "可分页查询证券公告目录",
        "cninfo_pdf": "可下载公告原文",
        "sse_etf_page": "页面可访问，接口需继续定位",
        "sse_etf_list_page": "页面可访问",
        "szse_etf_page": "页面可访问",
        "szse_fund_notice": "基金公告可访问",
        "szse_etf_size": "可返回ETF规模历史记录",
        "csindex_constituents": "可下载指数成分文件",
        "sina_company_events": "可读取公司事件辅助页",
        "fund_profile": "可读取ETF档案辅助页",
        "pbc_statistics": "统计入口可访问",
        "stats_bureau": "普通请求受限，需备用通道",
    }
    for name, result in audit["probes"].items():
        status = result.get("status", "error")
        lines.append(
            f"| {name} | {status} | {result.get('content_type', '—')} | "
            f"{result.get('bytes', '—')} | {conclusions.get(name, '待分析')} |"
        )
    lines += [
        "",
        "## 后续采集优先级",
        "",
        "1. 巨潮公告目录与PDF：股票事件和正式投资者关系资料的主来源。",
        "2. 深交所ETF规模/公告与中证指数成分文件：首批结构化ETF补充数据。",
        "3. 上交所ETF申赎清单和规模：页面可访问，但需定位稳定下载接口。",
        "4. 基金管理人资料、ETF季报持仓和费率：低频版本化采集。",
        "5. 新闻、互动易和宏观数据：在正式来源链稳定后接入，并保留来源等级。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    """Run the read-only local and public-source audit."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/databases/market.db")
    parser.add_argument("--output", default="docs/data_source_audit_20260920.md")
    args = parser.parse_args()
    audit = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "local": local_audit(Path(args.database)),
        "probes": probes(),
    }
    Path(args.output).write_text(markdown(audit), encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
