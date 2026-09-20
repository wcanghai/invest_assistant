"""使用 AKShare 生成 2004 年以来沪深 A 股退市股票 Markdown 清单。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import akshare as ak
import requests


SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_REPORT_URL = "https://www.szse.cn/api/report/ShowReport"
DEFAULT_START_DATE = date(2004, 1, 1)
DEFAULT_OUTPUT = Path("reports/market/AKShare退市股票列表_2004至今_20260902.md")


def parse_args() -> argparse.Namespace:
    # 解析起始日期和 Markdown 输出路径。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE.isoformat())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def normalize_date(value: object) -> date | None:
    # 将 AKShare 返回的日期对象或日期字符串统一转换为 date。
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    return date.fromisoformat(text[:10])


def normalize_code(value: object) -> str:
    # 将证券代码规范为六位字符串。
    text = str(value).strip().split(".", maxsplit=1)[0]
    return text.zfill(6)


def has_mojibake(value: object) -> bool:
    # 判断文本是否包含当前 AKShare/GBK 解码产生的替换字符。
    return "\ufffd" in str(value)


def fetch_with_akshare() -> list[dict[str, object]]:
    # 调用 AKShare 的沪深终止上市接口并统一字段。
    sh_frame = ak.stock_info_sh_delist(symbol="全部")
    sz_frame = ak.stock_info_sz_delist(symbol="终止上市公司")
    records: list[dict[str, object]] = []
    for values in sh_frame.itertuples(index=False, name=None):
        records.append(
            {
                "stock_code": normalize_code(values[0]),
                "stock_name": str(values[1]).strip(),
                "list_date": normalize_date(values[2]),
                "delist_date": normalize_date(values[3]),
                "exchange": "SH",
                "akshare_source": "stock_info_sh_delist",
            }
        )
    for values in sz_frame.itertuples(index=False, name=None):
        records.append(
            {
                "stock_code": normalize_code(values[0]),
                "stock_name": str(values[1]).strip(),
                "list_date": normalize_date(values[2]),
                "delist_date": normalize_date(values[3]),
                "exchange": "SZ",
                "akshare_source": "stock_info_sz_delist",
            }
        )
    return records


def fetch_sse_name_map() -> dict[str, str]:
    # 从 AKShare 所使用的上交所端点重新解码中文名称。
    params = {
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "isPagination": "true",
        "STOCK_CODE": "",
        "CSRC_CODE": "",
        "REG_PROVINCE": "",
        "STOCK_TYPE": "1,2,8",
        "COMPANY_STATUS": "3",
        "type": "inParams",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": "500",
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "1",
    }
    headers = {
        "Referer": "https://www.sse.com.cn/",
        "User-Agent": "Mozilla/5.0",
    }
    response = requests.get(SSE_QUERY_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    payload = json.loads(response.content.decode("gbk"))
    return {
        normalize_code(item["COMPANY_CODE"]): str(item["COMPANY_ABBR"]).strip()
        for item in payload.get("result", [])
    }


def read_inline_xlsx(content: bytes) -> list[list[str]]:
    # 从深交所 XLSX 的内联字符串工作表中读取二维文本。
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(content)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(worksheet)
    table: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        values: list[str] = []
        for cell in row.findall("x:c", namespace):
            text_node = cell.find(".//x:t", namespace)
            values.append(text_node.text.strip() if text_node is not None else "")
        table.append(values)
    return table


def fetch_szse_name_map() -> dict[str, str]:
    # 从 AKShare 所使用的深交所报表端点读取中文名称。
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1793_ssgs",
        "TABKEY": "tab2",
        "random": "0.6935816432433362",
    }
    response = requests.get(
        SZSE_REPORT_URL,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    table = read_inline_xlsx(response.content)
    return {normalize_code(row[0]): row[1] for row in table[1:] if len(row) >= 2}


def repair_names(records: list[dict[str, object]]) -> int:
    # 修复 AKShare 当前版本在本机产生的交易所 GBK 中文乱码。
    if not any(has_mojibake(record["stock_name"]) for record in records):
        return 0
    name_maps = {"SH": fetch_sse_name_map(), "SZ": fetch_szse_name_map()}
    repaired = 0
    for record in records:
        if not has_mojibake(record["stock_name"]):
            continue
        replacement = name_maps[str(record["exchange"])].get(str(record["stock_code"]))
        if replacement:
            record["stock_name"] = replacement
            repaired += 1
    return repaired


def is_a_share(record: dict[str, object]) -> bool:
    # 按当前项目 A 股范围排除沪深 B 股代码。
    code = str(record["stock_code"])
    exchange = str(record["exchange"])
    if exchange == "SH":
        return not code.startswith("900")
    return not code.startswith("200")


def filter_records(
    records: list[dict[str, object]],
    start_date: date,
) -> list[dict[str, object]]:
    # 筛选退市日不早于指定日期的沪深 A 股并去重排序。
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        delist_date = record["delist_date"]
        if not is_a_share(record) or not isinstance(delist_date, date):
            continue
        if delist_date < start_date or delist_date > date.today():
            continue
        key = (str(record["exchange"]), str(record["stock_code"]))
        unique[key] = record
    return sorted(
        unique.values(),
        key=lambda item: (
            item["delist_date"],
            item["exchange"],
            item["stock_code"],
        ),
    )


def markdown_escape(value: object) -> str:
    # 转义 Markdown 表格中的竖线和换行符。
    return str(value).replace("|", "\\|").replace("\n", " ")


def build_markdown(
    records: list[dict[str, object]],
    start_date: date,
    raw_count: int,
    repaired_count: int,
) -> str:
    # 构建包含口径、统计和明细的 Markdown 文本。
    exchange_counts = Counter(str(record["exchange"]) for record in records)
    year_counts = Counter(record["delist_date"].year for record in records)
    exchange_summary = (
        f"- 上交所：{exchange_counts.get('SH', 0)} 条；"
        f"深交所：{exchange_counts.get('SZ', 0)} 条。"
    )
    lines = [
        "# AKShare 获取的 2004 年以来沪深 A 股退市股票列表",
        "",
        f"> 生成日期：{date.today().isoformat()}",
        f"> 退市日期范围：{start_date.isoformat()} ～ {date.today().isoformat()}",
        "> 数据接口：AKShare `stock_info_sh_delist`、`stock_info_sz_delist`",
        "",
        "## 一、口径与结果",
        "",
        f"- AKShare 原始返回：{raw_count} 条；筛选后：{len(records)} 条。",
        exchange_summary,
        "- 以“终止/暂停上市日期不早于 2004-01-01”为筛选口径。",
        "- 按当前项目 A 股范围排除代码以 `900`、`200` 开头的沪深 B 股。",
        "- AKShare 暂无明确的北交所历史退市清单接口，因此本表只覆盖沪深市场。",
        "",
        "## 二、按退市年份统计",
        "",
        "| 退市年份 | 数量 |",
        "|---:|---:|",
    ]
    if repaired_count:
        lines.insert(
            13,
            f"- AKShare 返回的 {repaired_count} 条乱码简称已用其同源交易所端点恢复。",
        )
    for year in sorted(year_counts):
        lines.append(f"| {year} | {year_counts[year]} |")
    lines.extend(
        [
            "",
            "## 三、退市股票明细",
            "",
            "| 序号 | 股票代码 | 股票简称 | 交易所 | 上市日期 | 退市日期 | AKShare接口 |",
            "|---:|---|---|---|---|---|---|",
        ]
    )
    for index, record in enumerate(records, start=1):
        code = f"{record['stock_code']}.{'SH' if record['exchange'] == 'SH' else 'SZ'}"
        list_date = record["list_date"].isoformat() if record["list_date"] else "—"
        delist_date = record["delist_date"].isoformat()
        lines.append(
            "| "
            f"{index} | {code} | {markdown_escape(record['stock_name'])} | "
            f"{record['exchange']} | {list_date} | {delist_date} | "
            f"`{record['akshare_source']}` |"
        )
    lines.extend(
        [
            "",
            "## 四、数据来源",
            "",
            "- AKShare：https://github.com/akfamily/akshare",
            "- 上交所暂停/终止上市公司：",
            "  https://www.sse.com.cn/assortment/stock/list/delisting/",
            "- 深交所暂停/终止上市公司：",
            "  https://www.szse.cn/market/stock/suspend/index.html",
            "",
            "## 五、使用限制",
            "",
            "本表适合建立退市证券基础主表，但不包含终止上市决定日、退市原因、",
            "退市整理期起止日、最后交易日和退市后转板信息。正式无生存偏差回测时，",
            "还应结合交易所终止上市决定、摘牌公告和历史行情逐项补齐。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    # 调用 AKShare、修复编码、筛选并输出 Markdown 文件。
    args = parse_args()
    start_date = date.fromisoformat(args.start_date)
    raw_records = fetch_with_akshare()
    repaired_count = repair_names(raw_records)
    filtered_records = filter_records(raw_records, start_date)
    content = build_markdown(
        filtered_records,
        start_date,
        len(raw_records),
        repaired_count,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"OUTPUT={args.output.resolve()}")
    print(f"RAW={len(raw_records)} FILTERED={len(filtered_records)}")
    print(f"REPAIRED_NAMES={repaired_count}")


if __name__ == "__main__":
    main()
