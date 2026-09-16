import csv
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "security_pool.db"
OUT_DIR = ROOT / "doc"
CSV_PATH = OUT_DIR / "股票池基本财务指标Top20_20260901.csv"
MD_PATH = OUT_DIR / "股票池基本财务指标Top20_20260901.md"

METRICS = [
    ("total_market_cap_100m", "总市值最大", "亿元", "DESC", "规模指标，不代表投资质量"),
    ("float_market_cap_100m", "流通市值最大", "亿元", "DESC", "规模指标，不代表投资质量"),
    ("total_shares_10k", "总股本最大", "万股", "DESC", "规模指标，不代表投资质量"),
    ("float_shares_10k", "流通股本最大", "万股", "DESC", "规模指标，不代表投资质量"),
    ("free_float_shares_10k", "自由流通股本最大", "万股", "DESC", "规模指标，不代表投资质量"),
    ("pe_ttm", "市盈率最低（仅正值）", "倍", "ASC", "低估值候选；需排除利润周期和一次性收益影响"),
    ("pb_mrq", "市净率最低（仅正值）", "倍", "ASC", "低估值候选；金融、地产与重资产行业需分行业比较"),
    ("dividend_yield", "股息率最高", "%", "DESC", "收益指标；需检查分红可持续性"),
    ("turnover_rate", "换手率最高", "%", "DESC", "交易活跃度指标，不属于纯财务质量指标"),
    ("amount_prev_1d_10k", "前一交易日成交额最高", "万元", "DESC", "流动性指标，不属于纯财务质量指标"),
]


def fmt_value(value: float) -> str:
    if value is None:
        return ""
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def main() -> None:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row

    pool = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN is_active = 1 AND is_tradable = 1 THEN 1 ELSE 0 END) AS tradable,
               MAX(data_date) AS data_date
        FROM stock_current
        """
    ).fetchone()
    financial = conn.execute(
        """
        SELECT COUNT(DISTINCT stock_code) AS stocks,
               MAX(report_date) AS max_report_date,
               MAX(announce_date) AS max_announce_date
        FROM stock_financial_report
        """
    ).fetchone()
    latest_task = conn.execute(
        """
        SELECT status, started_at, finished_at
        FROM fetch_log
        WHERE task_type = 'STOCK_DATA_BULK_FULL'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    sections = []
    csv_rows = []
    coverage_rows = []

    for field, label, unit, direction, note in METRICS:
        positive_only = field in {"pe_ttm", "pb_mrq", "dividend_yield"}
        predicate = f"{field} IS NOT NULL"
        if positive_only:
            predicate += f" AND {field} > 0"

        coverage = conn.execute(
            f"""
            SELECT COUNT(*) AS valid_count
            FROM stock_current
            WHERE is_active = 1 AND is_tradable = 1 AND {predicate}
            """
        ).fetchone()["valid_count"]
        coverage_rows.append((label, coverage, pool["tradable"], note))

        rows = conn.execute(
            f"""
            SELECT stock_code, stock_name, COALESCE(industry_name, '') AS industry_name,
                   {field} AS metric_value
            FROM stock_current
            WHERE is_active = 1 AND is_tradable = 1 AND {predicate}
            ORDER BY {field} {direction}, stock_code ASC
            LIMIT 20
            """
        ).fetchall()

        section_lines = [
            f"## {label}",
            "",
            f"> 排序方向：{'从高到低' if direction == 'DESC' else '从低到高'}；单位：{unit}。{note}。",
            "",
            "| 排名 | 股票代码 | 股票名称 | 行业 | 指标值 |",
            "|---:|---|---|---|---:|",
        ]
        for rank, row in enumerate(rows, start=1):
            value = fmt_value(row["metric_value"])
            section_lines.append(
                f"| {rank} | {row['stock_code']} | {row['stock_name']} | {row['industry_name']} | {value} |"
            )
            csv_rows.append(
                [label, rank, row["stock_code"], row["stock_name"], row["industry_name"], row["metric_value"], unit, direction, note]
            )
        sections.append("\n".join(section_lines))

    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["指标", "排名", "股票代码", "股票名称", "行业", "指标值", "单位", "排序方向", "说明"])
        writer.writerows(csv_rows)

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    md = [
        "# 股票池基本财务指标 Top 20",
        "",
        f"> 生成时间：{generated_at}",
        f"> 股票池数据日期：{pool['data_date']}",
        f"> 股票池总数：{pool['total']:,}；本次分析范围：活跃且可交易股票 {pool['tradable']:,} 只。",
        "",
        "## 口径与限制",
        "",
        "本报告使用 `stock_current` 中对全股票池覆盖较完整的基础估值、股息、市值、股本与交易活跃度字段。市盈率和市净率只保留正值，避免亏损股负值在升序排名中被误判为低估。",
        "",
        f"完整财务报告表当前仅覆盖 {financial['stocks']:,} 只股票，最新报告期为 {financial['max_report_date']}、最新公告日为 {financial['max_announce_date']}；最近一次全量任务状态为 `{latest_task['status'] if latest_task else 'UNKNOWN'}`。因此本报告没有把 ROE、营收增长、净利润增长、现金流等不完整字段包装成全市场排名。",
        "",
        "“最大市值/股本”和“最高换手/成交额”只是规模或交易活跃度排名，不等同于基本面最优。单指标排名只能用于候选池初筛，不能直接作为买入结论。",
        "",
        "## 指标有效覆盖",
        "",
        "| 指标 | 有效股票数 | 分析股票数 | 说明 |",
        "|---|---:|---:|---|",
    ]
    for label, valid, total, note in coverage_rows:
        md.append(f"| {label} | {valid:,} | {total:,} | {note} |")
    md.extend(["", *sections, ""])
    MD_PATH.write_text("\n".join(md), encoding="utf-8")
    conn.close()

    print(MD_PATH)
    print(CSV_PATH)


if __name__ == "__main__":
    main()
