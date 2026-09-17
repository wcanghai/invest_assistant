"""生成日报快照并保存版本，不在浏览请求中隐式获取行情。"""

from datetime import datetime
from datetime import timedelta

from invest.reporting.common import CONFIG
from invest.reporting.common import REPORT_DB
from invest.reporting.common import SHANGHAI
from invest.reporting.common import SOURCE_DB
from invest.reporting.common import load_config
from invest.reporting.common import now_iso
from invest.reporting.local import read_local
from invest.storage.reports import connect
from invest.storage.reports import get_report
from invest.storage.reports import latest_observations
from invest.storage.reports import save_report


def markdown(data):
    # 将相同快照导出为可独立阅读的 Markdown。
    from invest.reporting.common import CATEGORIES

    lines = [f"# 投资日报 · {data['report_date']}", "",
             f"生成：{data['generated_at']} / 状态：{data['status']}", ""]
    s = data["summary"]
    lines += [f"行情覆盖 {s['covered']}/{s['expected']}；上涨 {s['up']}，"
              f"下跌 {s['down']}，平盘 {s['flat']}；"
              f"已覆盖成交额 {s['amount_100m']:,.2f} 亿元。", ""]
    for key, title in CATEGORIES.items():
        lines += [f"## {title}", "", "| 名称 | 代码 | 最新/收盘 | 涨跌幅% | 数据日期 | 状态 |",
                  "|---|---|---:|---:|---|---|"]
        for r in data["sections"][key]:
            values = [r.get(k) for k in ("name", "code", "close", "pct_change", "source_date")]
            values.append(r.get("error") or r.get("status"))
            cells = ["—" if v is None else str(v).replace("|", "/").replace("\n", " ")
                     for v in values]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["## 申购日历", ""]
    for row in data["offerings"]:
        lines.append(f"- {row['subscription_date']} {row['security_name']} "
                     f"({row['security_code']})")
    if not data["offerings"]:
        lines.append("本地暂无当前窗口的申购记录；不代表市场没有发行安排。")
    lines += ["", "## 数据说明", ""]
    lines.extend(f"- {note}" for note in data["warnings"])
    lines += ["", "## 网页来源", ""]
    for rows in data["sections"].values():
        for row in rows:
            if row.get("source_url"):
                lines.append(f"- {row['name']}：{row['source_url']} "
                             f"（采集：{row.get('observed_at', '—')}）")
    return "\n".join(lines) + "\n"


def generate(target=None, source_db=SOURCE_DB, report_db=REPORT_DB, config_path=CONFIG):
    # 基于已有采集结果生成报告，历史补报不混入之后采集的网页数据。
    config = load_config(config_path)
    generated = now_iso()
    report_date = target or generated[:10]
    data = read_local(config, report_date, source_db)
    data["a_share_date"] = data["report_date"]
    data["report_date"] = report_date
    cutoff = min(generated, report_date + "T23:59:59+08:00")
    conn = connect(report_db)
    try:
        conn.execute("BEGIN")
        external = latest_observations(conn, config, cutoff)
        conn.commit()
        for category, rows in external.items():
            for row in rows:
                observed = row.get("observed_at")
                source_date = row.get("source_date")
                if row.get("status") == "ok":
                    if not source_date or source_date > data["report_date"]:
                        row.update(status="stale", error="来源日期缺失或晚于报告日期")
                    elif observed and observed[:10] < data["report_date"]:
                        row.update(status="stale", error="并非报告当日采集，显示本地历史值")
                    elif category == "a_share_indices" and source_date < data["a_share_date"]:
                        row.update(status="stale", error="指数来源日期早于本地 A 股交易日")
                    elif category == "commodity_futures" and source_date < data["a_share_date"]:
                        row.update(status="stale", error="期货来源日期滞后，请检查合约是否停更")
                    elif source_date < data["report_date"] and category == "crypto_pairs":
                        row.update(status="stale", error="来源交易日期早于报告日期")
                    elif category == "us_stocks":
                        age = datetime.fromisoformat(cutoff) - datetime.fromisoformat(observed)
                        if age > timedelta(days=1):
                            row.update(status="stale", error="美股采集已超过一天")
                        elif source_date < (
                            datetime.fromisoformat(cutoff) - timedelta(days=4)
                        ).date().isoformat():
                            row.update(status="stale", error="美股来源交易日超过四天")
        data["sections"].update(external)
        data["generated_at"] = generated
        data["schema_version"] = 1
        data["config"] = config
        data["warnings"] = [
            "成交额为已覆盖活动 A 股汇总，不是资金净流入；停牌和未上市证券单列。",
            "价格分位为历史价格位置，不等于基本面估值；不足三年有效样本显示空值。",
            "股票价格分位使用复权价格；ETF 为未复权原价，分红和拆合份会影响可比性。",
            "外部市场展示网页采集时的报价，可能延迟或处于盘中，不冒充统一收盘价。",
            "期货沿用具体合约，涨跌通常相对昨结算；到期不自动换月。",
            "网页历史从首次采集开始积累，不使用后来行情填补过去日期。",
            "ETF 净值、折溢价、净申赎缺失，不生成这些指标的结论。",
            "申购日历来自本地数据，尚未补齐网页上市日、中签率和评级。",
        ]
        bad = [r for rows in data["sections"].values() for r in rows
               if r.get("status") != "ok"]
        s = data["summary"]
        complete = not (bad or s["missing"] or s["unknown"] or s["amount_missing"])
        data["status"] = "complete" if complete else "partial"
        if s["missing"]:
            data["warnings"].append("缺当日日线：" + "、".join(data["missing_stocks"]))
        report_id = save_report(conn, data, markdown(data))
        return get_report(conn, report_id)
    finally:
        conn.close()
