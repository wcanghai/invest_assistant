"""根据 SQLite 实时快照生成数据库数据情况 Markdown 报告。"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


TABLE_ORDER = (
    "stock_master",
    "stock_current",
    "stock_snapshot",
    "stock_sector_snapshot",
    "ipo_info",
    "convertible_bond_info",
    "etf_info",
    "etf_master",
    "etf_snapshot",
    "etf_daily",
    "etf_market_daily",
    "etf_data_sync_state",
    "stock_daily_bar",
    "stock_capital_daily",
    "stock_corporate_action",
    "stock_trade_daily",
    "stock_financial_report",
    "fetch_log",
)

TABLE_META = {
    "stock_master": ("证券主表", "每只证券一行，保存代码、市场、上市日期等低频静态属性。"),
    "stock_current": ("证券当前宽表", "每只证券一行，保存最近一次证券池刷新后的状态和估值快照。"),
    "stock_snapshot": ("证券每日快照", "按日期归档证券宽表，用于恢复历史证券池和避免未来数据。"),
    "stock_sector_snapshot": ("板块关系快照", "按日期保存股票与指数、行业、地区等板块的多对多关系。"),
    "ipo_info": ("新股新债申购", "保存申购代码、日期、价格、上限和发行市盈率等独立事件。"),
    "convertible_bond_info": ("可转债快照", "保存转债与正股关系、转股价、溢价率和规模等当前数据。"),
    "etf_info": ("ETF 当前映射", "保存最新指数与 ETF 映射、价格、IOPV、份额和规模。"),
    "etf_master": ("ETF 主表", "保存全部 ETF 的名称、上市日、跟踪标的和交易属性。"),
    "etf_snapshot": ("ETF 每日快照", "按日归档 ETF—指数映射、IOPV、份额、规模和折溢价。"),
    "etf_daily": ("ETF 日频数据", "保存 ETF 历史行情、净值、份额、规模、净申购和折溢价。"),
    "etf_market_daily": ("ETF 市场日频", "保存全市场 ETF 份额、规模及净申赎。"),
    "etf_data_sync_state": ("ETF 同步状态", "记录每只 ETF 已成功同步的数据截止日期。"),
    "stock_daily_bar": ("股票日线行情", "按股票和交易日保存 OHLCV、成交额、复权因子和涨跌幅。"),
    "stock_capital_daily": ("股票每日股本", "按股票和交易日保存总股本与流通股本。"),
    "stock_corporate_action": ("公司行为", "保存分红、送股、配股等除权除息事件。"),
    "stock_trade_daily": ("股票交易指标", "按日保存融资融券、北向持股、市值、质押和热度等指标。"),
    "stock_financial_report": ("财务报告", "按报告期和公告日保存利润、资产负债、现金流及财务比率。"),
    "fetch_log": ("数据任务日志", "记录全量、每日、修复任务的状态、时间、成功数和错误。"),
}

DATE_COLUMNS = {
    "stock_master": "first_seen_date",
    "stock_current": "data_date",
    "stock_snapshot": "snapshot_date",
    "stock_sector_snapshot": "snapshot_date",
    "ipo_info": "subscription_date",
    "convertible_bond_info": "data_date",
    "etf_info": "data_date",
    "etf_master": "first_seen_date",
    "etf_snapshot": "snapshot_date",
    "etf_daily": "trade_date",
    "etf_market_daily": "trade_date",
    "etf_data_sync_state": "last_success_date",
    "stock_daily_bar": "trade_date",
    "stock_capital_daily": "trade_date",
    "stock_corporate_action": "event_date",
    "stock_trade_daily": "trade_date",
    "stock_financial_report": "report_date",
    "fetch_log": "data_date",
}

STOCK_COLUMNS = {
    "stock_master": "stock_code",
    "stock_current": "stock_code",
    "stock_snapshot": "stock_code",
    "stock_sector_snapshot": "stock_code",
    "convertible_bond_info": "underlying_code",
    "etf_master": "etf_code",
    "etf_snapshot": "etf_code",
    "etf_daily": "etf_code",
    "etf_data_sync_state": "etf_code",
    "stock_daily_bar": "stock_code",
    "stock_capital_daily": "stock_code",
    "stock_corporate_action": "stock_code",
    "stock_trade_daily": "stock_code",
    "stock_financial_report": "stock_code",
}

FIELD_DESCRIPTIONS = {
    "id": "任务日志自增编号",
    "stock_code": "标准股票代码，格式为代码加市场后缀",
    "security_code": "发行证券代码",
    "bond_code": "可转债代码",
    "underlying_code": "可转债对应正股代码或 ETF 跟踪标的代码",
    "underlying_market_code": "ETF 跟踪标的的通达信市场代码",
    "index_code": "ETF 跟踪指数代码",
    "etf_code": "ETF 证券代码",
    "exchange": "交易所代码，如 SH、SZ、BJ",
    "security_kind": "通达信证券类型编号",
    "list_date": "上市日期",
    "initial_name": "首次采集时的证券名称",
    "stock_name": "股票当前名称",
    "security_name": "申购证券名称",
    "etf_name": "ETF 名称",
    "trade_unit": "最小交易单位",
    "min_price_tick": "最小价格变动单位",
    "price_precision": "价格小数位数",
    "first_seen_date": "系统首次发现证券的日期",
    "data_date": "当前数据所属日期",
    "snapshot_date": "快照归档日期",
    "trade_date": "交易日期",
    "event_date": "公司行为生效日期",
    "report_date": "财务报告期末日期",
    "announce_date": "财务报告公告日期",
    "subscription_date": "申购日期",
    "maturity_date": "可转债到期日期",
    "is_active": "是否仍在活动证券池，1是、0否",
    "is_all_a": "是否属于全部 A 股",
    "is_main_board": "是否属于沪深主板",
    "is_gem": "是否属于创业板",
    "is_star": "是否属于科创板",
    "is_bj_a": "是否属于北交所 A 股",
    "is_hs300": "是否为沪深300成份股",
    "is_zz500": "是否为中证500成份股",
    "is_zz1000": "是否为中证1000成份股",
    "is_a500": "是否为中证 A500 成份股",
    "is_stock_connect": "是否为沪深股通标的",
    "is_marginable": "是否为融资融券标的",
    "is_st": "是否为 ST 类股票",
    "is_delisting_board": "是否属于退市整理板",
    "is_suspended": "当日是否停牌",
    "has_convertible_bond": "是否存在关联可转债",
    "is_tradable": "按当前规则是否可交易",
    "industry_code": "所属行业代码",
    "industry_name": "所属行业名称",
    "region_code": "所属地区代码",
    "region_name": "所属地区名称",
    "sector_code": "板块或分类代码",
    "sector_name": "板块或分类名称",
    "sector_type": "板块类别，如指数、行业、地区",
    "source": "板块关系的数据来源",
    "total_shares_10k": "总股本，单位万股",
    "float_shares_10k": "流通股本，单位万股",
    "free_float_shares_10k": "自由流通股本，单位万股",
    "total_market_cap_100m": "总市值，单位亿元",
    "float_market_cap_100m": "流通市值，单位亿元",
    "turnover_rate": "换手率",
    "amount_prev_1d_10k": "前一交易日成交额，单位万元",
    "pe_ttm": "滚动市盈率",
    "pb_mrq": "最近报告期市净率",
    "dividend_yield": "股息率",
    "exclusion_reasons": "证券池排除原因集合",
    "issue_type": "发行或申购类型",
    "subscription_price": "申购价格",
    "subscription_code": "申购使用的交易代码",
    "max_subscription": "最大可申购数量",
    "issue_pe": "发行市盈率",
    "conversion_price": "可转债转股价格",
    "remaining_size": "可转债剩余规模",
    "bond_price": "可转债当前价格",
    "stock_price": "对应正股当前价格",
    "premium_rate": "转股溢价率",
    "conversion_value": "转股价值",
    "current_price": "当前价格",
    "previous_close": "上一交易日收盘价",
    "iopv": "ETF 基金份额参考净值",
    "shares_10k": "ETF 份额，单位万份",
    "size_100m": "ETF 规模，单位亿元",
    "size_10k": "ETF 规模，单位万元",
    "is_t0": "是否为 T+0 ETF",
    "unit_nav": "ETF 单位净值",
    "cumulative_nav": "ETF 累计净值",
    "net_subscription_10k": "ETF 净申购金额，单位万元",
    "premium_discount_pct": "ETF 相对单位净值的折溢价率",
    "total_shares_100m": "全市场 ETF 份额，单位亿份",
    "net_subscription_shares_100m": "全市场 ETF 净申赎，单位亿份",
    "total_size_100m": "全市场 ETF 规模，单位亿元",
    "net_subscription_size_100m": "全市场 ETF 净申赎，单位亿元",
    "data_domain": "同步数据域",
    "last_success_date": "最近成功同步截止日期",
    "raw_info_json": "ETF 基本信息接口原始 JSON",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "amount_10k": "成交额，单位万元",
    "forward_factor": "前复权因子",
    "pre_close": "前收盘价",
    "pct_change": "涨跌幅百分比",
    "is_trading": "是否有正常交易，1是、0否",
    "total_shares": "总股本或总股份数，原接口单位",
    "float_shares": "流通股份数，原接口单位",
    "action_type": "公司行为类型编号",
    "cash_bonus": "每股或方案现金分红值",
    "allot_price": "配股价格",
    "share_bonus": "送转股比例",
    "allotment": "配股比例或数量",
    "shareholder_count": "股东户数",
    "financing_balance_10k": "融资余额，单位万元",
    "securities_lending_balance_shares": "融券余额股数",
    "northbound_holding_shares": "北向资金持股数",
    "financing_buy_10k": "融资买入额，单位万元",
    "financing_repay_10k": "融资偿还额，单位万元",
    "financing_net_buy_10k": "融资净买入额，单位万元",
    "total_market_cap_10k": "总市值，单位万元",
    "dividend_yield_pct": "股息率百分比",
    "limit_status": "涨跌停状态编号",
    "limit_order_amount_10k": "涨跌停封单金额，单位万元",
    "pledge_ratio_pct": "股份质押比例百分比",
    "market_popularity_rank": "全市场人气排名",
    "industry_popularity_rank": "行业人气排名",
    "raw_metrics_json": "未展开的原始交易指标 JSON",
    "basic_eps": "基本每股收益",
    "deduct_eps": "扣除非经常损益后的每股收益",
    "book_value_per_share": "每股净资产",
    "roe_pct": "净资产收益率百分比",
    "total_assets": "资产总额",
    "total_liabilities": "负债总额",
    "total_equity": "所有者权益总额",
    "operating_cash_flow": "经营活动现金流量净额",
    "net_profit": "净利润",
    "revenue": "营业收入",
    "operating_profit": "营业利润",
    "parent_net_profit": "归属于母公司股东的净利润",
    "deduct_net_profit": "扣除非经常损益后的净利润",
    "revenue_growth_pct": "营业收入同比增长率",
    "net_profit_growth_pct": "净利润同比增长率",
    "gross_margin_pct": "毛利率百分比",
    "debt_ratio_pct": "资产负债率百分比",
    "float_a_shares": "流通 A 股股数",
    "raw_values_json": "未展开的原始财务指标 JSON",
    "task_type": "任务类型，如 FULL、DAILY、STOCK_DATA_DAILY",
    "status": "任务状态，如 RUNNING、SUCCESS、PARTIAL_SUCCESS、FAILED",
    "started_at": "任务开始时间",
    "finished_at": "任务结束时间",
    "stock_count": "任务成功处理的股票数",
    "error_message": "任务错误或部分失败摘要",
    "created_at": "记录创建时间",
    "updated_at": "记录最后更新时间",
    "archived_at": "快照归档时间",
}

DETAIL_TABLES = (
    "stock_daily_bar",
    "stock_capital_daily",
    "stock_corporate_action",
    "stock_trade_daily",
    "stock_financial_report",
)


def parse_args() -> argparse.Namespace:
    # 解析数据库路径和报告输出路径。
    parser = argparse.ArgumentParser(description="生成 SQLite 数据情况报告")
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def markdown_value(value: Any, limit: int = 100) -> str:
    # 将数据库值转换成适合 Markdown 表格的短文本。
    if value is None:
        return "NULL"
    text = str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def table_columns(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    # 返回指定表的 SQLite 字段定义。
    return list(connection.execute(f"PRAGMA table_info({table})"))


def sample_row(connection: sqlite3.Connection, table: str) -> sqlite3.Row | None:
    # 优先读取贵州茅台记录，否则读取日期最新的一条样例。
    columns = {row[1] for row in table_columns(connection, table)}
    if "stock_code" in columns:
        row = connection.execute(
            f"SELECT * FROM {table} WHERE stock_code=? LIMIT 1",
            ("600519.SH",),
        ).fetchone()
        if row is not None:
            return row
    date_column = DATE_COLUMNS.get(table)
    order = f" ORDER BY {date_column} DESC" if date_column else ""
    return connection.execute(f"SELECT * FROM {table}{order} LIMIT 1").fetchone()


def table_statistics(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, Any]:
    # 统计表记录数、证券覆盖数和日期范围。
    total = connection.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0]
    stock_column = STOCK_COLUMNS.get(table)
    covered = None
    if stock_column:
        covered = connection.execute(
            f"SELECT COUNT(DISTINCT {stock_column}) FROM {table} "
            f"WHERE {stock_column} IS NOT NULL"
        ).fetchone()[0]
    date_column = DATE_COLUMNS.get(table)
    date_range = (None, None)
    if date_column:
        date_range = connection.execute(
            f"SELECT MIN({date_column}), MAX({date_column}) FROM {table}"
        ).fetchone()
    return {
        "total": total,
        "covered": covered,
        "min_date": date_range[0],
        "max_date": date_range[1],
    }


def count_distribution(
    connection: sqlite3.Connection,
    table: str,
    stock_column: str,
) -> str:
    # 计算有数据股票的单股记录数最小值、中位数、平均值和最大值。
    counts = [
        row[0]
        for row in connection.execute(
            f"SELECT COUNT(1) FROM {table} WHERE {stock_column} IS NOT NULL "
            f"GROUP BY {stock_column}"
        )
    ]
    if not counts:
        return "无股票记录"
    return (
        f"最少 {min(counts):,}；中位数 {statistics.median(counts):,.1f}；"
        f"平均 {statistics.mean(counts):,.1f}；最多 {max(counts):,}"
    )


def append_overview(
    lines: list[str],
    connection: sqlite3.Connection,
) -> None:
    # 写入数据库概览和各表数据量汇总。
    lines.extend([
        "## 一、数据库总体情况",
        "",
        "数据库采用一个 SQLite 文件统一保存证券池、历史行情、交易指标、财务数据和任务日志。",
        "所有统计均来自同一个只读事务快照，因此即使后台任务正在写入，各章节数字也保持一致。",
        "",
        "| 表名 | 中文名称 | 记录数 | 覆盖股票 | 日期范围 |",
        "|---|---|---:|---:|---|",
    ])
    for table in TABLE_ORDER:
        stats = table_statistics(connection, table)
        covered = "—" if stats["covered"] is None else f"{stats['covered']:,}"
        date_range = "—"
        if stats["min_date"] is not None:
            date_range = f"{stats['min_date']} ～ {stats['max_date']}"
        lines.append(
            f"| `{table}` | {TABLE_META[table][0]} | {stats['total']:,} | "
            f"{covered} | {date_range} |"
        )


def append_table_details(
    lines: list[str],
    connection: sqlite3.Connection,
) -> None:
    # 为每张表写入用途、字段、样例和单股数据分布。
    lines.extend(["", "## 二、各表作用、字段与样例", ""])
    for index, table in enumerate(TABLE_ORDER, start=1):
        title, description = TABLE_META[table]
        stats = table_statistics(connection, table)
        sample = sample_row(connection, table)
        sample_values = dict(sample) if sample is not None else {}
        lines.extend([
            f"### 2.{index} `{table}` — {title}",
            "",
            description,
            "",
            f"当前记录数：**{stats['total']:,}**。",
        ])
        stock_column = STOCK_COLUMNS.get(table)
        if stock_column:
            lines.append(
                f"覆盖证券：**{stats['covered']:,}**；单证券记录分布："
                f"{count_distribution(connection, table, stock_column)}。"
            )
        lines.extend([
            "",
            "| 字段 | SQLite 类型 | 约束 | 含义 | 样例 |",
            "|---|---|---|---|---|",
        ])
        for column in table_columns(connection, table):
            name = column[1]
            constraints = []
            if column[3]:
                constraints.append("NOT NULL")
            if column[5]:
                constraints.append(f"PK({column[5]})")
            constraint_text = "、".join(constraints) or "—"
            description_text = FIELD_DESCRIPTIONS.get(name, "业务字段")
            example = markdown_value(sample_values.get(name))
            lines.append(
                f"| `{name}` | {column[2]} | {constraint_text} | "
                f"{description_text} | {example} |"
            )
        lines.append("")


def detail_counts(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    # 汇总所有已有明细数据股票在五张历史表中的记录数。
    query = """
        WITH detail_codes AS (
            SELECT stock_code FROM stock_daily_bar
            UNION SELECT stock_code FROM stock_capital_daily
            UNION SELECT stock_code FROM stock_corporate_action
            UNION SELECT stock_code FROM stock_trade_daily
            UNION SELECT stock_code FROM stock_financial_report
        ),
        bar AS (
            SELECT stock_code, COUNT(1) AS value FROM stock_daily_bar GROUP BY stock_code
        ),
        capital AS (
            SELECT stock_code, COUNT(1) AS value
            FROM stock_capital_daily GROUP BY stock_code
        ),
        action AS (
            SELECT stock_code, COUNT(1) AS value
            FROM stock_corporate_action GROUP BY stock_code
        ),
        trade_data AS (
            SELECT stock_code, COUNT(1) AS value
            FROM stock_trade_daily GROUP BY stock_code
        ),
        finance AS (
            SELECT stock_code, COUNT(1) AS value
            FROM stock_financial_report GROUP BY stock_code
        )
        SELECT d.stock_code, c.stock_name, COALESCE(c.is_zz500, 0),
               COALESCE(bar.value, 0), COALESCE(capital.value, 0),
               COALESCE(action.value, 0), COALESCE(trade_data.value, 0),
               COALESCE(finance.value, 0)
        FROM detail_codes d
        LEFT JOIN stock_current c ON c.stock_code=d.stock_code
        LEFT JOIN bar ON bar.stock_code=d.stock_code
        LEFT JOIN capital ON capital.stock_code=d.stock_code
        LEFT JOIN action ON action.stock_code=d.stock_code
        LEFT JOIN trade_data ON trade_data.stock_code=d.stock_code
        LEFT JOIN finance ON finance.stock_code=d.stock_code
        ORDER BY c.is_zz500 DESC, d.stock_code
    """
    return list(connection.execute(query))


def append_stock_counts(
    lines: list[str],
    connection: sqlite3.Connection,
) -> None:
    # 写入每只已有历史数据股票在五张明细表中的记录数。
    master_count = connection.execute("SELECT COUNT(1) FROM stock_master").fetchone()[0]
    rows = detail_counts(connection)
    lines.extend([
        "## 三、逐股票数据量",
        "",
        f"证券主表共有 **{master_count:,}** 只证券；其中 **{len(rows):,}** 只已在至少一张股票历史明细表中有数据。",
        "未出现在下表的证券，其五张股票明细表记录数均为0。证券主表和当前宽表原则上每只证券各1条；",
        "快照表按日期每只证券最多1条；板块关系表因一只股票可属于多个板块，所以每只股票记录数不同。",
        "",
        "| 股票代码 | 股票名称 | 中证500 | 日线 | 股本 | 公司行为 | 交易指标 | 财务报告 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in rows:
        values = [markdown_value(value) for value in row]
        lines.append(
            f"| `{values[0]}` | {values[1]} | {values[2]} | {int(row[3]):,} | "
            f"{int(row[4]):,} | {int(row[5]):,} | {int(row[6]):,} | "
            f"{int(row[7]):,} |"
        )
    lines.append("")


def append_usage(lines: list[str]) -> None:
    # 写入各类表的推荐使用方式和典型 SQL。
    lines.extend([
        "## 四、表数据应该如何使用",
        "",
        "### 4.1 构建某日可交易证券池",
        "",
        "回测必须使用历史快照，不能用当前宽表回看历史，否则会产生幸存者偏差和未来数据。",
        "",
        "```sql",
        "SELECT s.stock_code, s.stock_name, s.industry_name, s.is_zz500",
        "FROM stock_snapshot s",
        "WHERE s.snapshot_date = '2026-08-31'",
        "  AND s.is_active = 1",
        "  AND s.is_tradable = 1;",
        "```",
        "",
        "### 4.2 获取行情并进行复权",
        "",
        "`stock_daily_bar` 是收益率、动量、波动率、成交量和技术指标的基础表。使用价格做跨期比较时，",
        "应明确选择原始收盘价还是结合 `forward_factor` 计算的前复权价格。",
        "",
        "```sql",
        "SELECT trade_date, open, high, low, close, volume, amount_10k, forward_factor",
        "FROM stock_daily_bar",
        "WHERE stock_code = '600519.SH'",
        "ORDER BY trade_date;",
        "```",
        "",
        "### 4.3 计算市值和股本因子",
        "",
        "将 `stock_capital_daily` 按 `stock_code + trade_date` 与日线连接，可计算市值、流通市值、",
        "换手相关指标，并识别增发、回购或股本变化。",
        "",
        "### 4.4 处理分红送配和公司行为",
        "",
        "`stock_corporate_action` 是事件表，不保证每只股票都有记录。可用于检查复权、构建分红策略、",
        "识别送转和配股事件，不应把“无记录”直接解释为数据缺失。",
        "",
        "### 4.5 使用交易指标",
        "",
        "`stock_trade_daily` 适合构建融资余额变化、北向持股变化、质押风险、封单和人气因子。",
        "不同指标的起始日期和发布频率不同，建模前需要逐字段检查非空率；未标准化字段保留在",
        "`raw_metrics_json`，可在需要时二次展开。",
        "",
        "### 4.6 使用财务报告",
        "",
        "财务数据必须按 `announce_date` 与行情做时点连接，而不是只按 `report_date` 连接，否则会",
        "把尚未公告的数据提前用于回测。相同报告期可能因不同公告日保留多条修订记录。",
        "",
        "```sql",
        "SELECT report_date, announce_date, revenue, parent_net_profit, roe_pct",
        "FROM stock_financial_report",
        "WHERE stock_code = '600519.SH'",
        "ORDER BY announce_date;",
        "```",
        "",
        "### 4.7 板块和指数成份关系",
        "",
        "使用 `stock_sector_snapshot` 按快照日期选择当时的指数、行业或地区成员。当前状态筛选可直接",
        "使用 `stock_current.is_zz500` 等标志；历史回测应使用板块快照，避免成份股穿越。",
        "",
        "### 4.8 数据质量和任务监控",
        "",
        "每次同步后检查 `fetch_log`。只有 `SUCCESS` 表示范围内全部成功；`PARTIAL_SUCCESS` 需要读取",
        "`error_message` 并对失败股票补采；`RUNNING` 表示统计时任务尚未结束。建议同时运行",
        "`python -m stock_data.main check-data --check-profile daily --date YYYY-MM-DD` "
        "检查当日数据与同步完整度；使用 `--check-profile full` 执行全库完整检查。",
        "",
        "## 五、使用注意事项",
        "",
        "- `NULL` 不一定是错误：很多指标只在特定股票、日期或公告存在。",
        "- 不同表的金额和股本单位不同，使用前应按字段说明统一单位。",
        "- 日频因子通常按 `stock_code + trade_date` 对齐；财务因子按公告日向后生效。",
        "- 证券池快照和板块快照用于历史时点，当前宽表只适合实时筛选。",
        "- 每日任务采用回补窗口和 UPSERT，可安全重复执行，但仍需关注任务日志中的部分失败。",
    ])


def generate_report(database_path: Path) -> str:
    # 在一致性只读事务中生成完整 Markdown 文本。
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    running = list(
        connection.execute(
            "SELECT id, task_type, data_date, started_at FROM fetch_log "
            "WHERE status='RUNNING' ORDER BY id"
        )
    )
    size_mb = database_path.stat().st_size / 1024 / 1024
    lines = [
        "# 当前 SQLite 数据库数据情况",
        "",
        f"> 统计时间：{generated_at}",
        f"> 数据库：`{database_path}`",
        f"> 文件大小：{size_mb:,.2f} MiB",
    ]
    if running:
        task_text = "；".join(
            f"#{row['id']} {row['task_type']}({row['data_date']})"
            for row in running
        )
        lines.extend([
            f"> 统计时仍在运行的任务：{task_text}",
            "> 本报告是生成时的一致性快照，后台任务结束后数据量可能继续增加。",
        ])
    else:
        lines.append("> 统计时没有状态为 RUNNING 的数据任务。")
    lines.append("")
    append_overview(lines, connection)
    append_table_details(lines, connection)
    append_stock_counts(lines, connection)
    append_usage(lines)
    connection.rollback()
    connection.close()
    return "\n".join(lines) + "\n"


def main() -> None:
    # 生成报告并以 UTF-8 编码写入目标 Markdown 文件。
    args = parse_args()
    database_path = Path(args.db).resolve()
    output_path = Path(args.output).resolve()
    report = generate_report(database_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
