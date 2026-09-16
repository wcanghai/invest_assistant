"""只读审计本地数据并生成十类经典策略的截面初筛报告。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "classic_strategies_20260914"
OUT.mkdir(parents=True, exist_ok=True)
CON = sqlite3.connect(f"file:{ROOT / 'data/security_pool.db'}?mode=ro", uri=True)
CON.execute("BEGIN")


def read(sql, params=()):
    # 在同一只读事务中读取数据，保持本次筛选一致。
    return pd.read_sql_query(sql, CON, params=params)


def raw_number(raw, field):
    # 读取明确指定的原始财务字段，缺失不得补零。
    return pd.to_numeric(raw.get(field), errors="coerce")


def markdown(frame):
    # 无需额外依赖生成可阅读的 Markdown 表格。
    copy = frame.copy()
    for col in copy.select_dtypes(include="number"):
        copy[col] = copy[col].map(lambda v: "—" if pd.isna(v) else f"{v:.2f}")
    lines = ["| " + " | ".join(copy.columns) + " |"]
    lines.append("|" + "---|" * len(copy.columns))
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in copy.values)
    return "\n".join(lines)


ASOF = read("SELECT MAX(snapshot_date) d FROM stock_snapshot").iloc[0, 0]
pool = read(
    "SELECT s.*,m.list_date FROM stock_snapshot s JOIN stock_master m USING(stock_code) "
    "WHERE snapshot_date=?", (ASOF,),
).set_index("stock_code")
calendar = read(
    "SELECT trade_date FROM etf_calendar WHERE verified=1 AND trade_date<=? "
    "UNION SELECT DISTINCT trade_date FROM stock_daily_bar WHERE trade_date<=? "
    "ORDER BY trade_date", (ASOF, ASOF),
)["trade_date"].tolist()
window = calendar[-253:]
bars = read(
    "SELECT stock_code,trade_date,close,volume,amount_10k,forward_factor,is_trading "
    "FROM stock_daily_bar WHERE trade_date BETWEEN ? AND ?",
    (window[0], ASOF),
)
bars["adj"] = bars["close"] * bars["forward_factor"]
valid = (bars.close > 0) & (bars.forward_factor > 0) & (bars.volume > 0)
valid &= bars.is_trading.eq(1)
prices = bars.loc[valid].pivot(index="trade_date", columns="stock_code", values="adj")
prices = prices.reindex(window)
amounts = bars.loc[valid].pivot(
    index="trade_date", columns="stock_code", values="amount_10k",
).reindex(window)
returns = prices.pct_change(fill_method=None)
pool["close"] = bars.loc[bars.trade_date.eq(ASOF)].set_index("stock_code")["close"]
pool["adv20_10k"] = amounts.tail(20).mean()
pool["obs20"] = amounts.tail(20).count()
pool["price_coverage"] = prices.count() / len(window)
pool["vol_obs"] = returns.tail(126).count()
pool["vol126_pct"] = returns.tail(126).std() * np.sqrt(252) * 100
pool["mom12_1_pct"] = (prices.iloc[-22] / prices.iloc[0] - 1) * 100
pool["mom6_1_pct"] = (prices.iloc[-22] / prices.iloc[-127] - 1) * 100
pool["return1_pct"] = (prices.iloc[-1] / prices.iloc[-22] - 1) * 100
pool["return6_pct"] = (prices.iloc[-1] / prices.iloc[-127] - 1) * 100
pool["max_abs_return_pct"] = returns.abs().max() * 100
financial = read(
    "SELECT stock_code,report_date,announce_date,basic_eps,deduct_eps,roe_pct,"
    "revenue_growth_pct,net_profit_growth_pct,debt_ratio_pct,raw_values_json "
    "FROM stock_financial_report WHERE report_date>='2023-01-01' AND announce_date<=? "
    "AND announce_date>report_date ORDER BY stock_code,report_date,announce_date", (ASOF,),
).drop_duplicates(["stock_code", "report_date"], keep="last")
raw_fields = {
    "ocf_cumulative": "FN107", "profit_cumulative": "FN134",
    "deduct_growth_pct": "FN191", "capex": "FN114",
}
raw_records = []
for value in financial.raw_values_json:
    raw = json.loads(value)
    raw_records.append({name: raw_number(raw, fn) for name, fn in raw_fields.items()})
for name in raw_fields:
    financial[name] = [row[name] for row in raw_records]
financial = financial.drop(columns="raw_values_json")
financial["cash_profit"] = financial.ocf_cumulative / financial.profit_cumulative
latest = financial.groupby("stock_code").tail(1).set_index("stock_code")
pool = pool.join(latest)
annual = financial.loc[financial.report_date.isin(
    ["2023-12-31", "2024-12-31", "2025-12-31"]
)]
groups = annual.groupby("stock_code")
pool["annual_count"] = groups.size()
pool["annual_eps_min"] = groups.basic_eps.min()
pool["annual_roe_min"] = groups.roe_pct.min()
pool["annual_roe_mean"] = groups.roe_pct.mean()
pool["annual_roe_std"] = groups.roe_pct.std()
pool["annual_ocf_min"] = groups.ocf_cumulative.min()
pool["annual_growth_mean"] = groups.revenue_growth_pct.mean()
pool["annual_growth_count"] = groups.revenue_growth_pct.count()
pool["annual_growth_min"] = groups.revenue_growth_pct.min()
pool["peg"] = pool.pe_ttm / pool.net_profit_growth_pct
steps = []
mask = pd.Series(True, index=pool.index)
for label, condition in [
    ("活动A股", pool.is_active.eq(1) & pool.is_all_a.eq(1)),
    ("非ST、非退市整理、非停牌且可交易", pool.is_st.eq(0)
     & pool.is_delisting_board.eq(0) & pool.is_suspended.eq(0) & pool.is_tradable.eq(1)),
    ("上市至少365自然日且上市日期已知",
     pd.to_datetime(pool.list_date) <= pd.Timestamp(ASOF) - pd.Timedelta(days=365)),
    ("当日有效交易行情", pool.index.isin(prices.columns[prices.iloc[-1].notna()])),
    ("最近20个市场交易日至少18日有效行情", pool.obs20.ge(18)),
    ("有效日平均成交额不少于2000万元", pool.adv20_10k.ge(2000)),
]:
    mask &= pd.Series(condition, index=pool.index).fillna(False)
    steps.append({"步骤": label, "剩余股票": int(mask.sum())})
base = pool.loc[mask].copy()
fresh = base.report_date.eq("2026-06-30")
history = base.annual_count.eq(3) & base.annual_eps_min.gt(0)
profit = base.basic_eps.gt(0) & base.deduct_eps.gt(0)
nonfinancial = ~base.industry_code.str.startswith(("X50", "X51"), na=True)
price_ok = base.price_coverage.ge(.95) & base.vol_obs.ge(114)
price_ok &= base.max_abs_return_pct.le(35)
results = {}
rules = {}
counts = {}


def screen(key, label, condition, factors, description, columns):
    # 先按硬条件筛选，再在合格候选内按百分位加权评分，完整保存排名。
    eligible = base.loc[condition.fillna(False)].copy()
    eligible = eligible.dropna(subset=list(factors))
    score = pd.Series(0.0, index=eligible.index)
    for field, weight in factors.items():
        score += eligible[field].rank(pct=True, ascending=weight > 0) * abs(weight)
    eligible["score"] = score * 100 / sum(abs(w) for w in factors.values())
    eligible = eligible.reset_index().sort_values(
        ["score", "stock_code"], ascending=[False, True], kind="stable",
    )
    eligible.insert(0, "rank", range(1, len(eligible) + 1))
    eligible.to_csv(OUT / f"{key}_all.csv", index=False, encoding="utf-8-sig")
    selected = eligible.head(10)
    selected.to_csv(OUT / f"{key}_top10.csv", index=False, encoding="utf-8-sig")
    results[label] = selected[["rank", "stock_code", "stock_name", "industry_name",
                               "score", *columns]]
    rules[label] = description
    counts[label] = len(eligible)


screen("value", "价值投资（低估值初筛）", fresh & profit
       & base.pe_ttm.between(0.01, 20) & base.pb_mrq.between(0.01, 2),
       {"pe_ttm": -.6, "pb_mrq": -.4},
       "最新半年报基本及扣非EPS均>0；0<PE≤20、0<PB≤2；"
       "按低PE百分位60%+低PB百分位40%排名。跨行业原始排名，不作行业中性化。",
       ["pe_ttm", "pb_mrq"])
screen("growth", "成长投资（历史成长初筛）", fresh & profit & history
       & base.revenue_growth_pct.between(15, 100)
       & base.net_profit_growth_pct.between(15, 100)
       & base.deduct_growth_pct.between(10, 100)
       & base.annual_growth_count.eq(3) & base.annual_growth_mean.gt(5),
       {"revenue_growth_pct": .4, "deduct_growth_pct": .4, "annual_growth_mean": .2},
       "三年年报EPS均>0；半年报基本及扣非EPS>0；营收、净利润同比15%—100%，"
       "扣非利润同比10%—100%；三年营收增速均值>5%；"
       "收入增长40%+扣非增长40%+三年营收增速均值20%。",
       ["revenue_growth_pct", "net_profit_growth_pct", "deduct_growth_pct"])
screen("quality", "质量投资（财务质量初筛）", fresh & profit & history & nonfinancial
       & base.annual_roe_min.ge(10) & base.debt_ratio_pct.between(0, 60)
       & base.annual_ocf_min.gt(0) & base.profit_cumulative.gt(0)
       & base.cash_profit.ge(.8),
       {"annual_roe_mean": .4, "annual_roe_std": -.2,
        "debt_ratio_pct": -.2, "cash_profit": .2},
       "排除银行及非银金融；三年盈利、每年ROE≥10%、每年经营现金流>0；"
       "最新资产负债率0%—60%、累计净利润>0、累计经营现金流/净利润≥0.8；"
       "三年平均ROE40%+低ROE标准差20%+低负债率20%+现金盈利比20%。",
       ["annual_roe_mean", "annual_roe_std", "debt_ratio_pct", "cash_profit"])
screen("garp", "GARP（历史增速代理版）", fresh & profit & history
       & base.revenue_growth_pct.between(10, 100)
       & base.net_profit_growth_pct.between(15, 60) & base.deduct_growth_pct.ge(10)
       & base.pe_ttm.between(5, 35) & base.peg.between(.2, 1.5),
       {"peg": -.6, "revenue_growth_pct": .2, "deduct_growth_pct": .2},
       "三年盈利；半年报基本及扣非EPS>0；营收同比10%—100%、净利润同比15%—60%、"
       "扣非同比≥10%；PE在5—35，PEG在0.2—1.5；"
       "PEG=PE/净利润同比百分数，非预测PEG；低PEG60%+营收增长20%+扣非增长20%。",
       ["pe_ttm", "net_profit_growth_pct", "peg"])
screen("dividend", "红利策略（股息率初筛）", fresh & profit & history
       & base.dividend_yield.between(3, 12) & base.pe_ttm.between(.01, 25),
       {"dividend_yield": .8, "pe_ttm": -.2},
       "三年盈利；半年报基本及扣非EPS>0；快照股息率3%—12%、0<PE≤25；"
       "高股息率80%+低PE20%。未验证连续分红、一次性特别股息及派息率。",
       ["dividend_yield", "pe_ttm"])
screen("momentum", "动量策略（价格动量初筛）", price_ok
       & base.mom12_1_pct.gt(0) & base.mom6_1_pct.gt(0),
       {"mom12_1_pct": .5, "mom6_1_pct": .5},
       "253个市场交易日价格覆盖≥95%、126日相邻日收益至少114个、"
       "已观测单日绝对收益≤35%；12减1月及6减1月动量均>0；各占50%。"
       "复权价格比值计算，跳过最近21个交易日；不复制MSCI指数。",
       ["mom12_1_pct", "mom6_1_pct", "vol126_pct"])
screen("reversal", "反转策略（超跌修复观察版）", price_ok & fresh & profit
       & base.return6_pct.le(-15) & base.return1_pct.gt(0)
       & base.revenue_growth_pct.gt(0) & base.net_profit_growth_pct.gt(0),
       {"return6_pct": -.6, "return1_pct": .4},
       "满足价格覆盖门槛；半年报基本及扣非EPS>0、营收和净利润同比>0；"
       "近126日跌幅≥15%、近21日涨幅>0；半年超跌程度60%+近月反弹40%。"
       "只识别超跌且已有反弹，不能据此判定困境已反转。",
       ["return6_pct", "return1_pct", "net_profit_growth_pct"])
screen("small", "小市值策略（盈利过滤版）", fresh & profit
       & base.total_market_cap_100m.gt(0), {"total_market_cap_100m": -1},
       "半年报基本及扣非EPS>0、总市值>0；按快照总市值从小到大排序。"
       "盈利过滤仅指当期半年报，不保证TTM盈利。"
       "沪深北均参与，没有额外排除北交所。",
       ["total_market_cap_100m", "adv20_10k", "pe_ttm"])
screen("lowvol", "低波动策略（个股波动初筛）", price_ok & base.vol126_pct.gt(0),
       {"vol126_pct": -1},
       "满足价格覆盖门槛；按近126个交易日有效相邻复权收益标准差×√252升序；"
       "不填充缺失价格，不把跨缺失日涨跌算作单日收益；非最小方差组合优化。",
       ["vol126_pct", "vol_obs", "price_coverage"])

coverage_cols = ["pe_ttm", "pb_mrq", "dividend_yield", "total_market_cap_100m",
                 "basic_eps", "roe_pct", "revenue_growth_pct", "deduct_growth_pct",
                 "ocf_cumulative", "profit_cumulative", "capex", "annual_roe_mean",
                 "mom12_1_pct", "mom6_1_pct", "vol126_pct"]
coverage = pd.DataFrame({"字段": coverage_cols,
                         "有效非空股票数": [int(pool[x].notna().sum())
                                           for x in coverage_cols]})
recent = bars.groupby("trade_date").size().reindex(calendar[-25:], fill_value=0)
audit = {
    "asof": ASOF, "pool_count": len(pool), "base_count": len(base),
    "latest_bar_count": int(bars.trade_date.eq(ASOF).sum()),
    "financial_latest_periods": pool.report_date.value_counts(dropna=False).to_dict(),
    "window_dates": {"start12": window[0], "start6": window[-127],
                     "skip1": window[-22], "end": ASOF},
    "recent_bar_counts": recent.to_dict(), "eligible_counts": counts,
    "coverage": coverage.to_dict("records"), "steps": steps,
    "rules": rules, "source_db": str(ROOT / "data/security_pool.db"),
    "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
}
audit["snapshot_update"] = read(
    "SELECT MIN(archived_at) earliest,MAX(archived_at) latest "
    "FROM stock_snapshot WHERE snapshot_date=?", (ASOF,),
).to_dict("records")[0]
audit["bar_update"] = read(
    "SELECT MIN(updated_at) earliest,MAX(updated_at) latest "
    "FROM stock_daily_bar WHERE trade_date=?", (ASOF,),
).to_dict("records")[0]
pool.to_csv(OUT / "features_all.csv", encoding="utf-8-sig")
(OUT / "audit.json").write_text(
    json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
)
sections = [
    f"# 十类经典选股策略：数据支持与初筛（{ASOF}）",
    f"数据来源：本地 security_pool.db 的同一只读事务。证券快照{len(pool)}只，"
    f"当日日线{audit['latest_bar_count']}只，共同筛选池{len(base)}只。"
    "排名是指定规则下的截面候选，不是预期收益排名，也未进行回测。",
    "## 数据边界",
    "财务最新报告期以2026年半年报为门槛，只使用公告日不晚于筛选日的数据。"
    "库内财务显式revenue、parent_net_profit、operating_cash_flow存在口径疑点："
    "例如茅台2026半年报FN234与FN107分别约437.81亿、706.91亿，"
    "因此不直接调用既有TTM拼接函数。现金流统一读取FN107，净利润读取FN134，"
    "增长率读取供应商FN183/FN184/FN191，ROE使用报告期值，年度稳定性只用年报。"
    "供应商增长率与原始报表的一致性仍需公告级核验。",
    "股票行情在9月7日、9月9日整日缺失，9月8日覆盖不全。"
    "日历使用本地已验证ETF交易日历与股票实际交易日并集。"
    "价格策略属于有覆盖门槛的初筛：端点必须真实存在，不插值、不前填。"
    "低波动仅统计真实相邻市场交易日，可能因缺失行情而低估风险。"
    "复权因子使用库内值，尚未逐个公司行为核验；35%异常过滤不能证明完全无误。",
    "9月14日证券快照采集于18:55，日线更新于18:55—19:18，属于盘后采集。"
    "本次按数据库标注日期分析，未另行向行情源逐笔核对；"
    "财报修订版本、历史证券状态也不足以支持严格长期无偏回测。",
    "## 共同筛选门槛", markdown(pd.DataFrame(steps)),
    "评分：硬条件全部通过后，指定指标在该策略合格集合内计算百分位，"
    "按明确权重加权为0—100分；同分按代码升序。不同策略分数不可横比。"
    "不限制同一行业入选数量，因此名单可能高度集中，不能直接当作分散组合。",
    "缺失行情与低流动性分别统计。9月8日没有采集到行情的股票，即使真实成交活跃，"
    "也可能因20日有效观测不足被排除。这使结果只代表当前数据合格子集，"
    "不能称为无遗漏的全A股Top10；应在补齐行情后重新筛选。",
    "## 十种策略的支持判断与缺口",
    "|策略|当前支持程度|仍缺少或需验证的数据|\n|---|---|---|\n"
    "|价值|支持低PE/PB初筛|自由现金流完整口径、正常化利润、内在价值假设；"
    "FN114虽有存储但尚未完成FCF对账|\n"
    "|成长|支持历史财报成长初筛|未来收入/盈利预测、市场空间、竞争优势证据|\n"
    "|质量|支持财务质量初筛|ROIC所需投入资本及税后经营利润口径、治理与护城河证据|\n"
    "|GARP|仅支持历史PEG代理|一致预期盈利、未来3—5年增速及预测修订历史|\n"
    "|红利|仅支持快照股息率初筛|股息率观察周期、逐次分红方案及实施状态、"
    "拆股调整后的DPS、特别股息标识、同口径派息率与连续性验证|\n"
    "|动量|支持覆盖受限的价格初筛|缺失交易日行情、逐事件复权验证|\n"
    "|反转/困境反转|仅支持超跌修复观察|困境原因、债务到期结构、重组进展、"
    "订单及经营拐点证据；不能确认困境反转|\n"
    "|小市值|支持当前总市值排序|交易权限与可执行报价；完整历史退市样本用于回测|\n"
    "|低波动|支持覆盖受限的个股波动初筛|连续日线；组合版另需完整协方差及约束|\n"
    "|事件驱动|不支持，不生成Top10|并购/分拆/重组等公告全文及发布时间、"
    "事件类型、条款、状态、对价、完成概率与失败损失；"
    "已有除权除息表不等于待完成事件数据库|",
    "## 字段覆盖（全证券快照，非空不等于有效或正值）", markdown(coverage),
    "## 最近25个市场交易日的股票行情覆盖",
    markdown(recent.rename("行情股票数").rename_axis("交易日").reset_index()),
]
rename = {
    "rank": "排名", "stock_code": "代码", "stock_name": "名称",
    "industry_name": "行业", "score": "规则分", "pe_ttm": "PE",
    "pb_mrq": "PB", "dividend_yield": "股息率%", "peg": "历史PEG",
    "revenue_growth_pct": "营收同比%", "net_profit_growth_pct": "净利润同比%",
    "deduct_growth_pct": "扣非同比%", "annual_roe_mean": "三年均ROE%",
    "annual_roe_std": "ROE标准差", "debt_ratio_pct": "负债率%",
    "cash_profit": "累计现金盈利比", "mom12_1_pct": "12减1月动量%",
    "mom6_1_pct": "6减1月动量%", "return6_pct": "半年涨跌%",
    "return1_pct": "近月涨跌%", "vol126_pct": "年化波动%",
    "vol_obs": "有效日收益数", "price_coverage": "价格覆盖比例",
    "total_market_cap_100m": "总市值亿元", "adv20_10k": "日均成交万元",
}
for label, result in results.items():
    sections += [f"## {label}", rules[label],
                 f"合格候选{counts[label]}只，列出前{len(result)}只；不足10只不放宽规则。",
                 markdown(result.rename(columns=rename))]
sections += ["## 输出与复现",
             "features_all.csv保存全股票特征，audit.json保存规则和审计；"
             "每类*_all.csv保存完整合格排名，*_top10.csv保存前10。"
             "全部名单来源于本地计算，网页仅用于策略定义参考，没有混入外部股票报价。",
             "方法参考：[MSCI动量](https://www.msci.com/indexes/group/momentum-indexes)、"
             "[MSCI红利](https://www.msci.com/indexes/group/high-dividend-yield-indexes)。"
             "这里采用自定义初筛规则，不声称复刻上述指数。"]
sections += ["## 名单解读",
             "价值前10中9只是银行，源于低PE/PB偏好；这不是分散组合。"
             "红利前10中9只最新净利润同比下降，股息率高不能证明后续分红安全。"
             "动量名单允许亏损公司进入，体现价格信号；质量名单也允许当期利润下降。"
             "小市值名单中的启迪设计、中达安PE为负，虽半年报盈利，TTM仍可能亏损。"
             "上述特点均未事后修改筛选条件来隐藏，后续投资筛选应另加相应约束。"]
(OUT / "report.md").write_text("\n\n".join(sections), encoding="utf-8")
combined = pd.concat([
    pd.read_csv(OUT / f"{key}_top10.csv").assign(strategy=key)
    for key in ["value", "growth", "quality", "garp", "dividend", "momentum",
                "reversal", "small", "lowvol"]
], ignore_index=True)
combined.to_csv(OUT / "all_top10.csv", index=False, encoding="utf-8-sig")
CON.rollback()
CON.close()
print(json.dumps({"asof": ASOF, "base": len(base), "counts": counts}, ensure_ascii=False))
