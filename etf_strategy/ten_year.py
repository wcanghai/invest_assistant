"""Reproducible ten-year per-strategy reports, phase ledgers and static figures."""

import argparse
import copy
import gzip
import hashlib
import html
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .backtest import backtest, metrics, rebalance_days
from .config import LEGACY_STRATEGIES as STRATEGIES, load_config
from .data import Dataset, audit
from .storage import clean, digest
from .strategies import features, target


NAMES = {
    "dual_momentum": "多资产双动量与逆波动权重",
    "trend": "多资产趋势跟踪",
    "risk_parity": "风险平价与趋势过滤",
    "strategic": "战略多资产配置与季度再平衡",
    "factor_rotation": "纯风格ETF动量轮动",
}
RULES = {
    "dual_momentum": "126/252日动量均值为正且高于MA200；Top3；60日逆波动权重；"
                     "不足3只按n/3投资，其余留现金。未启用额外波动率目标或权重上限。",
    "trend": "股票、国债、黄金、商品四类各25%；类内等权；MA200过滤；每月调仓。",
    "risk_parity": "126日窗口、至少100个共同样本；10%对角收缩；类内和类间等风险贡献；"
                   "MA200过滤后不重新归一化；每月调仓。",
    "strategic": "四类资产各25%，类内等权；季度末再平衡；缺失类别份额留现金；"
                 "每月进行持仓数据质量检查。",
    "factor_rotation": "同一市场价值、质量、低波三种纯风格均通过准入后，按中期动量和"
                       "MA200筛选；最多两个风格各50%，不足留现金，不放宽流动性。",
}
REASONS = {
    "classification_unverified_or_wrong_pool": "分类未核验或不属于本策略池",
    "not_listed_or_unknown_listing": "尚未上市或上市日期未知",
    "no_history": "没有可用历史",
    "insufficient_history": "有效历史少于253日",
    "poor_coverage": "近期行情覆盖不足95%",
    "invalid_signal_day": "信号日无有效行情",
    "insufficient_recent_trading": "20日内有效成交不足18日",
    "unknown_liquidity": "成交额存在未知缺失",
    "low_liquidity": "ADV20低于2000万元",
    "duplicate_exposure": "重复暴露被更高流动性产品替代",
}
SOURCES = [
    ("趋势研究：长期证据不等于本ETF多头实现的收益承诺",
     "https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/AQR-JPM-Fall-2017.pdf"),
    ("波动率管理研究：风险暴露调整仍需样本外检验",
     "https://www.nber.org/papers/w22208"),
    ("资产配置与再平衡：目标是维持风险结构",
     "https://www.investor.gov/introduction-investing/getting-started/asset-allocation"),
]


def write_json(path, value):
    # Write strict UTF-8 JSON with no nonportable NaN values.
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def phase_ledger(result, dataset):
    # Partition every NAV observation once, attributing net P&L to actual held/traded assets.
    curve = pd.DataFrame(result["curve"]).set_index("date")
    decisions = result["signals"]
    dates = list(curve.index)
    phases, assets = [], []
    price_cache = {}

    def price_at(code, day):
        # Match the engine's last valid raw close when a valuation is stale.
        key = code, day
        if key not in price_cache:
            bar = dataset.bars[code].loc[:day]
            valid = bar.loc[bar.valid, "close"]
            price_cache[key] = float(valid.iloc[-1]) if len(valid) else 0.0
        return price_cache[key]

    for index in range(len(decisions) + 1):
        decision = decisions[index - 1] if index else None
        signal_day = decision["signal_date"] if decision else None
        first = dates[0] if signal_day is None else next((d for d in dates if d > signal_day), None)
        last = decisions[index]["signal_date"] if index < len(decisions) else dates[-1]
        if first is None or first > last:
            continue
        begin = curve.loc[signal_day] if signal_day else None
        finish = curve.loc[last]
        start_nav = float(begin.nav) if begin is not None else 1.0
        start_hold = begin.holdings if begin is not None else {}
        end_hold = finish.holdings
        trades = [t for t in result["trades"] if first <= t["date"] <= last]
        universe = decision["universe"] if decision else []
        eligible = [r for r in universe if r["eligible"]]
        codes = sorted(set(start_hold) | set(end_hold) | {t["code"] for t in trades})
        net = 0.0
        for code in codes:
            related = [t for t in trades if t["code"] == code]
            bought = sum(t["quantity"] * t["price"] for t in related if t["side"] == "buy")
            sold = sum(t["quantity"] * t["price"] for t in related if t["side"] == "sell")
            fees = sum(t["fee"] for t in related)
            p0 = price_at(code, signal_day) if signal_day else 0
            p1 = price_at(code, last)
            v0 = begin.get("holding_values", {}).get(code, start_hold.get(code, 0) * p0) \
                if begin is not None else 0
            v1 = finish.get("holding_values", {}).get(code, end_hold.get(code, 0) * p1)
            dividends = sum(e["cash_entitlement"] for e in result.get("events", [])
                            if e["code"] == code and first <= e["date"] <= last)
            pnl = v1 - v0 + sold - bought - fees + dividends
            net += pnl
            assets.append({"phase": index, "signal_date": signal_day, "start": first,
                           "end": last, "etf_code": code, "start_quantity": start_hold.get(code, 0),
                           "end_quantity": end_hold.get(code, 0), "buy_notional": bought,
                           "sell_notional": sold, "fees": fees, "net_pnl_nav_units": pnl,
                           "phase_return_contribution": pnl / start_nav,
                           "asset_price_change": p1 / p0 - 1 if p0 > 0 else None})
        residual = float(finish.nav) - start_nav - net
        if abs(residual) > 1e-9:
            assets.append({"phase": index, "signal_date": signal_day, "start": first,
                           "end": last, "etf_code": "UNALLOCATED_CASH_OR_ACTION",
                           "net_pnl_nav_units": residual,
                           "phase_return_contribution": residual / start_nav})
        period = curve.loc[first:last]
        phases.append({
            "phase": index, "signal_date": signal_day or "initial_cash", "start": first,
            "end": last, "sessions": len(period), "status": decision["status"] if decision
            else "initial_cash", "reason": decision.get("reason", "") if decision else "",
            "eligible_codes": ";".join(r["etf_code"] for r in eligible),
            "asset_classes": ";".join(sorted({r.get("asset_class", "") for r in eligible})),
            "target_weights": json.dumps(decision["weights"] if decision else {}, sort_keys=True),
            "target_cash": decision["cash_weight"] if decision else 1.0,
            "actual_end_holdings": json.dumps(end_hold, sort_keys=True),
            "start_nav": start_nav, "end_nav": float(finish.nav),
            "phase_return": float(finish.nav) / start_nav - 1,
            "cumulative_return": float(finish.nav) - 1,
            "average_cash_weight": float(period.cash_weight.mean()),
            "trade_count": len(trades), "fees": sum(t["fee"] for t in trades),
            "reconciliation_residual": residual,
        })
    phase_frame = pd.DataFrame(phases)
    compounded = float((1 + phase_frame.phase_return).prod())
    if not np.isclose(compounded, curve.nav.iloc[-1], rtol=1e-10, atol=1e-10):
        raise AssertionError("Phase returns do not reconcile with NAV")
    return phase_frame, pd.DataFrame(assets)


def screening_tables(result, dataset, folder):
    # Export every security's exclusions plus compact seeded candidates and cumulative gates.
    detail, steps = [], []
    relevant = set(dataset.classes.loc[dataset.classes.pool.eq(
        "factor" if result["strategy"] == "factor_rotation" else "base"), "etf_code"])
    path = folder / "all_screening.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as handle:
        for number, decision in enumerate(result["signals"], 1):
            day = decision["signal_date"]
            universe = pd.DataFrame(decision["universe"])
            universe["phase"] = number
            universe.to_csv(handle, index=False, header=number == 1)
            alive = list(decision["universe"])
            steps.append({"phase": number, "date": day, "step": "起始主表",
                          "remaining": len(alive)})
            for reason, label in REASONS.items():
                alive = [r for r in alive if reason not in r["reasons"]]
                steps.append({"phase": number, "date": day, "step": label,
                              "remaining": len(alive)})
            codes = sorted(relevant & set(dataset.bars))
            diagnostic = features(dataset, day, codes).to_dict("index")
            for row in decision["universe"]:
                code = row["etf_code"]
                if code not in relevant:
                    continue
                feature = diagnostic.get(code, {})
                weight = decision["weights"].get(code, 0)
                detail.append({
                    "phase": number, "signal_date": day, "etf_code": code,
                    "name": row["etf_name"], "pool_eligible": row["eligible"],
                    "exclusion_reasons": ";".join(REASONS.get(r, r) for r in row["reasons"]),
                    "history": row.get("valid_history"), "adv20_10k": row.get("adv20_10k"),
                    "coverage": row.get("coverage"), **feature,
                    "selected": weight > 0, "target_weight": weight,
                    "strategy_status": decision["status"],
                    "strategy_reason": decision.get("reason", ""),
                })
            steps.append({"phase": number, "date": day, "step": "最终目标持仓",
                          "remaining": len(decision["weights"])})
    detail_frame, step_frame = pd.DataFrame(detail), pd.DataFrame(steps)
    detail_frame.to_csv(folder / "phase_candidates.csv", index=False, encoding="utf-8-sig")
    step_frame.to_csv(folder / "screening_steps.csv", index=False, encoding="utf-8-sig")
    return detail_frame, step_frame


def rolling_and_exposures(result, dataset):
    # Measure rolling consistency, concentration and realized annual asset exposure.
    frame = pd.DataFrame(result["curve"])
    nav = frame.nav
    returns = nav.pct_change(fill_method=None).fillna(0)
    frame["rolling_252_return"] = nav / nav.shift(252) - 1
    frame["drawdown"] = nav / nav.cummax().clip(lower=1) - 1
    weights = []
    for row in frame.to_dict("records"):
        values = {}
        for code, qty in row["holdings"].items():
            bars = dataset.bars[code].loc[:row["date"]]
            valid = bars.loc[bars.valid, "close"]
            value = row.get("holding_values", {}).get(code)
            if value is None:
                value = qty * valid.iloc[-1] if len(valid) else 0
            values[code] = value / row["nav"]
        values["RECEIVABLE"] = row.get("receivables", 0) / row["nav"]
        weights.append({"date": row["date"], **values, "CASH": row["cash_weight"]})
    exposure = pd.DataFrame(weights).fillna(0).set_index("date")
    rolling = frame.rolling_252_return.dropna()
    invested = frame.cash_weight.lt(.999999)
    diagnostics = {
        "positive_rolling_year_fraction": float(rolling.gt(0).mean()) if len(rolling) else None,
        "worst_rolling_year": float(rolling.min()) if len(rolling) else None,
        "best_rolling_year": float(rolling.max()) if len(rolling) else None,
        "annualized_volatility": float(returns.std() * np.sqrt(252)),
        "active_sessions": int(invested.sum()), "total_sessions": len(frame),
        "max_single_asset_weight": float(exposure.drop(columns="CASH").max().max())
        if len(exposure.columns) > 1 else 0,
        "average_asset_weights": exposure.mean().to_dict(),
        "stale_valuation_days": sum(bool(r["stale_codes"]) for r in result["curve"]),
    }
    return frame, exposure, diagnostics


def plot_curves(result, benchmark, comparison, folder, enhanced, exposure):
    # Plot cumulative return, drawdown and asset exposure with explicit non-investable labels.
    plt.rcParams.update({"font.family": ["Microsoft YaHei", "DejaVu Sans"],
                         "axes.unicode_minus": False, "font.size": 10})
    name = result["strategy"]
    x = pd.to_datetime(enhanced.date)
    fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1, 1]})
    inactive = result["first_active_signal"] is None
    axes[0].plot(x, (enhanced.nav - 1) * 100, color="#126b73", linewidth=2,
                 linestyle="--" if inactive else "-",
                 label="未准入：现金账本，非策略业绩" if inactive else NAMES[name])
    for reference, label, color in ((benchmark, "沪深300ETF买入持有（原价）", "#ab7548"),
                                    (comparison, "战略配置对照", "#9995b2")):
        if reference and reference["strategy"] != name:
            frame = pd.DataFrame(reference["curve"])
            axes[0].plot(pd.to_datetime(frame.date), (frame.nav - 1) * 100,
                         label=label, color=color, linewidth=1.1, alpha=.8)
    axes[0].set_title(NAMES[name] + "｜十年探索回测", loc="left", fontsize=16, pad=16)
    axes[0].set_ylabel("累计收益（%）")
    axes[0].legend(loc="upper left", fontsize=9)
    axes[1].fill_between(x, enhanced.drawdown * 100, 0, color="#b65152", alpha=.65)
    axes[1].set_ylabel("回撤（%）")
    if len(exposure.columns):
        axes[2].stackplot(x, *[exposure[c].to_numpy() * 100 for c in exposure],
                          labels=exposure.columns, alpha=.85)
        axes[2].legend(loc="upper left", ncol=3, fontsize=8)
    axes[2].set_ylabel("实际仓位（%）")
    axes[2].set_ylim(0, 105)
    for axis in axes:
        axis.grid(alpha=.18)
        axis.spines[["top", "right"]].set_visible(False)
    fig.text(.08, .015, "未完整计入分红、历史存续偏差；无杠杆、未设置个人仓位上限；"
             "图中结果不是实盘业绩。", fontsize=10, color="#6a5656")
    fig.tight_layout(rect=(0, .04, 1, 1))
    fig.savefig(folder / "returns.png", dpi=160)
    fig.savefig(folder / "returns.svg")
    plt.close(fig)


def percent(value):
    # Render missing values honestly instead of presenting inactive cash as investment returns.
    return "不适用" if value is None or pd.isna(value) else f"{value:.2%}"


def md_table(headers, rows):
    # Produce portable Markdown tables without optional tabulate dependencies.
    def cell(value):
        # Escape pipes and newlines inside table cells.
        return str(value).replace("|", "/").replace("\n", " ")

    return "\n".join(["|" + "|".join(map(cell, headers)) + "|",
                       "|" + "|".join("---" for _ in headers) + "|"] +
                      ["|" + "|".join(map(cell, row)) + "|" for row in rows])


def evaluation(result, diagnostics, phases, tests):
    # Tie recommendations to measured failure modes and separate hypotheses from evidence.
    name, metric = result["strategy"], result["metrics"]
    if not result["first_active_signal"]:
        return [
            "十年内按原规则没有产生合格持仓，不能据此报告策略年化收益、Sharpe或低风险优势。",
            "主要约束是纯质量候选2021年才上市，以及三类代表的历史流动性不足；"
            "这不是已验证的因子收益失败，而是当前可交易样本的可行性失败。",
            "优先扩展并独立核验同一纯风格的其他ETF；不能用红利质量等混合暴露直接替代。",
            "若用指数总收益序列延长历史，必须另建指数研究报告，扣除可实现成本，"
            "并与真实ETF可交易期分开；本次没有拼接或制造上市前数据。",
            "降低2000万元门槛属于另一项容量实验，需要账户规模和盘口验证，不能为获得"
            "好看的回测而改写本次规则。",
        ]
    focus = {
        "dual_momentum": "逆波动率会把较多资金放到低波动债券；高集中度会使表面上的"
                         "多资产组合主要依赖利率风险。可先做单资产上限、波动率下限的独立实验。",
        "trend": "MA200在反复震荡和快速反弹中可能增加空仓或换向。可预注册双均线缓冲区、"
                 "多窗口趋势投票实验，但先检查换手是否真的下降。",
        "risk_parity": "风险贡献均衡不等于经济风险均衡；短历史协方差和债券低波动会影响权重。"
                       "优先比较63/126/252日估计窗口及类别上限，不用回测最高Sharpe挑参数。",
        "strategic": "作为简单基准，它能暴露复杂信号是否值得额外换手。可比较季度调仓与"
                     "权重偏离阈值再平衡，但增加规则必须计入成本并留出验证期。",
    }
    live = phases[phases.signal_date.ne("initial_cash")]
    best, worst = live.loc[live.phase_return.idxmax()], live.loc[live.phase_return.idxmin()]
    base_cagr = metric["cagr"]
    doubled = tests["double_cost"]["cagr"]
    delayed = tests["one_session_delay"]["cagr"]
    return [
        f"本样本净年化为{percent(base_cagr)}，最大回撤{percent(metric['max_drawdown'])}；"
        f"这些数字受未计分红和当前存续样本限制，不能直接判断长期预期收益。",
        f"最差调仓期为{worst['start']}—{worst['end']}，收益{percent(worst.phase_return)}；"
        f"最好为{best['start']}—{best['end']}，收益{percent(best.phase_return)}。"
        "对应候选、成交与单ETF贡献均可在阶段明细中追溯。",
        f"252交易日滚动窗口盈利比例为{percent(diagnostics['positive_rolling_year_fraction'])}，"
        f"最差滚动年收益为{percent(diagnostics['worst_rolling_year'])}。"
        "连续窗口重叠，因此这一比例不是独立成功概率。",
        f"费用和滑点翻倍后年化为{percent(doubled)}，比基准变化"
        f"{(doubled - base_cagr) * 100:+.2f}个百分点；额外延迟一交易日成交后年化"
        f"为{percent(delayed)}。这是固定规则敏感性测试，不是参数优化。",
        f"单ETF实际权重峰值{percent(diagnostics['max_single_asset_weight'])}，平均现金比例"
        f"{percent(metric['average_cash_weight'])}。" + focus[name],
        f"最长处于前高以下为{metric['longest_drawdown_sessions']}个交易日；"
        f"期末未修复回撤持续{metric['ongoing_drawdown_sessions']}日。"
        "资金占用时间与最大跌幅一样影响真实持有体验。",
    ]


def render_report(result, phases, candidates, diagnostics, tests, folder, benchmark):
    # Write one complete Chinese report with all rebalance stages and downloadable raw evidence.
    name, metric = result["strategy"], result["metrics"]
    active = result["first_active_signal"] is not None
    valid_metric = metric if active else {}
    notes = evaluation(result, diagnostics, phases, tests)
    lines = [f"# {NAMES[name]}：十年独立报告", "",
             f"窗口：{metric['start']}—{metric['end']}；交易日数：{diagnostics['total_sessions']}。",
             "", "**数据等级：探索研究。原始价格未完整计入分红；当前存续小样本、日线成交假设。**",
             "", "## 1. 规则、样本与测试口径", "", RULES[name], "",
             "初始净值1，分数份额；单边费用3bp、滑点5bp，现金收益0；收盘信号最早次日"
             "开盘执行。未指定个人资金、未使用融资、未启用账户容量和最低佣金模拟。", "",
             "基础池为沪深300、纳指100、5年国债、黄金、豆粕期货五只ETF；因子池为"
             "上证180价值、MSCI中国A股国际质量、中证500行业中性低波三只ETF。"
             "未上市或历史不足253日的资产不能提前加入。没有全市场完整历史名单，不能消除"
             "生存者偏差。商品类别在早期缺位，风险预算结构并非十年完全一致。", "",
             "年度首尾不足整年的部分保留实际收益，不额外年化；十年起始至首个调仓日为"
             "初始现金阶段。每阶段从信号日之后首个交易日起，至下一信号日收盘止；阶段收益"
             "包含实际持仓隔夜变化与成交成本。最后一期是未结束调仓周期。", "",
             "## 2. 收益与风险", "",
             md_table(["指标", "结果"], [
                 ["是否存在合格活跃区间", "是" if active else "否；现金曲线不是策略业绩"],
                 ["首个有效持仓信号", result["first_active_signal"] or "不适用"],
                 ["累计收益", percent(result["curve"][-1]["nav"] - 1) if active else "不适用"],
                 ["CAGR", percent(valid_metric.get("cagr"))],
                 ["最大回撤", percent(valid_metric.get("max_drawdown"))],
                 ["Sharpe（无风险=0）", valid_metric.get("sharpe_zero_rf", "不适用")],
                 ["Calmar", valid_metric.get("calmar", "不适用")],
                 ["成交笔数", len(result["trades"])],
                 ["单边换手总计（买卖额/2）", metric["one_way_turnover"]],
                 ["平均现金比例", percent(metric["average_cash_weight"])],
                 ["实际有仓位日数", diagnostics["active_sessions"]],
                 ["陈旧价格估值日数", diagnostics["stale_valuation_days"]],
             ]), "", "![收益、回撤与实际仓位](returns.png)", "",
             "曲线对照为相同原价口径的沪深300ETF买入持有、战略配置；沪深300对照首日产生"
             "一次买入信号、次日开盘买入，含相同费用滑点。它的股票风险与多资产组合不同。",
             "", "### 年度收益", "",
             md_table(["年份", "策略收益", "沪深300ETF对照"], [
                 [year, percent(ret) if active else "不适用（未准入）",
                  percent(benchmark["metrics"]["annual_returns"].get(year))]
                 for year, ret in metric["annual_returns"].items()]), "",
             "## 3. 固定规则敏感性与开发/验证分段", "",
             md_table(["测试", "CAGR", "最大回撤", "Sharpe"], [
                 [label, percent(m["cagr"]) if active else "不适用",
                  percent(m["max_drawdown"]) if active else "不适用",
                  m.get("sharpe_zero_rf") if active else "不适用"]
                 for label, m in tests.items() if "cagr" in m]), "",
             "零成本、成本翻倍、额外延迟一交易日均复用已冻结信号。60/40历史分段保持前期"
             "持仓，不重新挑参数；所有历史已可见，不能称为真正前瞻样本外。测试仅比较执行"
             "敏感性，没有按结果更换ETF或降低第五种策略的门槛。", "",
             "## 4. 策略评价与改进", ""]
    lines += [f"{i}. {note}" for i, note in enumerate(notes, 1)]
    lines += ["", "## 5. 每次调仓阶段的筛选、持仓与收益", "",
              "下表逐期列出全部阶段。目标权重不等于实际成交；实际份额、未成交告警和"
              "净收益贡献见CSV。净收益贡献采用阶段损益/阶段初净值，各资产贡献之和"
              "与组合阶段收益核对；单ETF价格涨幅不被当作组合贡献。", "",
              md_table(["期", "信号日", "持有区间", "通过准入ETF", "目标权重", "阶段收益",
                        "累计收益", "状态"], [
                  [r.phase, r.signal_date, f"{r.start}—{r.end}", r.eligible_codes or "无",
                   "; ".join(f"{c}:{w:.1%}" for c, w in json.loads(r.target_weights).items())
                   or "现金", percent(r.phase_return), percent(r.cumulative_return), r.status]
                  for r in phases.itertuples()]), "",
              "### 每一步筛选和ETF明细文件", "",
              "- [阶段收益与实际期末持仓](phases.csv)",
              "- [每阶段逐ETF筛选、动量、趋势与目标权重](phase_candidates.csv)",
              "- [每阶段每一步筛选剩余数量](screening_steps.csv)",
              "- [完整主表逐ETF排除记录（gzip压缩CSV）](all_screening.csv.gz)",
              "- [逐ETF阶段净损益与贡献](phase_contributions.csv)",
              "- [日净值与回撤](daily.csv)", "- [全部模拟成交](trades.csv)",
              "- [实际每日权重](actual_weights.csv)", "- [参数与数据摘要](manifest.json)", "",
              "## 6. 其他思考与结论边界", "",
              "- 十年观测长度不等于十年稳定资产覆盖。可交易资产集合变化本身会改变组合风险；"
              "下一步宜在完整类别共同存续区间另做等条件检验。",
              "- 回测前先修复分红、拆合份及历史存续名单。对国债和红利类资产，遗漏分红"
              "可能明显改变收益比较与趋势信号；不能简单给当前年化统一加一个股息率。",
              "- 参数改进应事先登记，保留一段新数据做前瞻模拟；一次只调整一个模块，"
              "评价净收益、回撤、回本时间和集中度，不追逐样本内最优。",
              "- 长期持有与择时有不同风险敞口。现金多或债券多导致的低回撤，不能直接"
              "解释为更高的选时能力；应在相近风险预算下再次比较。",
              "- 第五种策略的未准入状态是一项可执行性结论；不能用现金直线计算出"
              "零风险优势，也不能用后来上市ETF回填2016年的交易。", "",
              "### 研究依据", ""]
    lines += [f"- [{label}]({url})" for label, url in SOURCES]
    text = "\n".join(lines) + "\n"
    (folder / "report.md").write_text(text, encoding="utf-8")
    # Self-contained HTML keeps the long phase table usable with browser search and print.
    import base64

    image = base64.b64encode((folder / "returns.png").read_bytes()).decode()
    body = f"<h1>{html.escape(NAMES[name])}：十年独立报告</h1>"
    body += f"<p class='note'>原价探索回测｜{metric['start']}—{metric['end']}｜非实盘业绩</p>"
    body += f"<p>{html.escape(RULES[name])}</p>"
    body += f"<img alt='收益、回撤与仓位' src='data:image/png;base64,{image}'>"
    body += "<h2>评价与改进</h2><ol>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes)
    body += "</ol><h2>完整报告正文</h2><pre>" + html.escape(text) + "</pre>"
    body += "<h2>逐阶段收益与目标</h2><div class='scroll'>" + phases.to_html(index=False) + "</div>"
    body += "<h2>逐ETF筛选明细</h2><div class='scroll'>" + candidates.to_html(index=False) + "</div>"
    page = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>ETF十年报告</title>"
    page += "<style>body{max-width:1200px;margin:40px auto;padding:0 24px;font:16px/1.7 "
    page += "'Microsoft YaHei',sans-serif;color:#24343c}h1,h2{color:#126b73}img{width:100%}"
    page += ".note{background:#fff2db;padding:16px}pre{white-space:pre-wrap;font:14px/1.7 "
    page += "'Microsoft YaHei',sans-serif;overflow-wrap:anywhere}"
    page += ".scroll{overflow:auto;max-height:620px}"
    page += "table{border-collapse:collapse;font-size:12px}td,th{padding:8px;border:1px solid #ddd}"
    page += "th{position:sticky;top:0;background:#eaf3f2}</style><body>" + body + "</body></html>"
    (folder / "report.html").write_text(page, encoding="utf-8")


def run(output, db, start, end):
    # Run the frozen five policies with shared signals and independent full reporting.
    output.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    dataset = Dataset(db, cfg, end)
    names = dataset.master.set_index("etf_code").etf_name.to_dict()
    if not dataset.dates.size or dataset.end < end:
        raise ValueError("Requested end date is unavailable")
    cached = {name: {} for name in STRATEGIES}
    quarters = rebalance_days(dataset, "strategic")
    monthly = [d for d in sorted(rebalance_days(dataset, "trend")) if start <= d <= end]
    for number, day in enumerate(monthly, 1):
        base, factor = dataset.universe(day), dataset.universe(day, "factor")
        for name in STRATEGIES:
            if name != "strategic" or day in quarters:
                cached[name][day] = target(dataset, day, name,
                                           factor if name == "factor_rotation" else base)
        if number % 12 == 0:
            message = f"Prepared {number}/{len(monthly)} monthly screening snapshots: {day}"
            print(message, flush=True)
    dates = [d for d in dataset.dates if start <= d <= end]
    reference_signal = {"signal_date": dates[0], "weights": {"510300.SH": 1},
                        "cash_weight": 0, "status": "ok", "quality_level": "exploratory"}
    benchmark = backtest(dataset, "trend", start, end, signals={dates[0]: reference_signal})
    benchmark["strategy"] = "buy_hold_csi300"
    results = {}
    for name in STRATEGIES:
        print(f"Running ten-year baseline: {name}", flush=True)
        results[name] = backtest(dataset, name, start, end, signals=cached[name])
    source_audit = audit(db)
    source_hash = digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in Path(__file__).parent.glob("*.py")})
    summary_rows = []
    for name in STRATEGIES:
        print(f"Stress tests and detailed report: {name}", flush=True)
        result = results[name]
        folder = output / name
        folder.mkdir(exist_ok=True)
        tests = {"baseline": result["metrics"]}
        for label, multiplier in (("zero_cost", 0), ("double_cost", 2)):
            dataset.config = {**cfg, "fee_bps": cfg["fee_bps"] * multiplier,
                              "slippage_bps": cfg["slippage_bps"] * multiplier}
            stress = backtest(dataset, name, start, end, signals=cached[name])
            tests[label] = stress["metrics"]
        dataset.config = cfg
        delayed = {}
        for day, decision in cached[name].items():
            i = list(dataset.dates).index(day)
            if i + 1 < len(dataset.dates):
                delayed[dataset.dates[i + 1]] = decision
        tests["one_session_delay"] = backtest(dataset, name, start, end,
                                               signals=delayed)["metrics"]
        cut = dates[int(len(dates) * .6)]
        for label, condition in (("development_60pct", lambda d: d < cut),
                                  ("validation_40pct", lambda d: d >= cut)):
            rows = [r for r in result["curve"] if condition(r["date"])]
            prior = [r for r in result["curve"] if r["date"] < rows[0]["date"]]
            anchor = prior[-1]["nav"] if prior else 1
            tests[label] = metrics([{**r, "nav": r["nav"] / anchor} for r in rows])
        phases, contributions = phase_ledger(result, dataset)
        candidates, steps = screening_tables(result, dataset, folder)
        enhanced, exposure, diagnostics = rolling_and_exposures(result, dataset)
        phases.to_csv(folder / "phases.csv", index=False, encoding="utf-8-sig")
        if contributions.empty:
            contributions = pd.DataFrame(columns=[
                "phase", "signal_date", "start", "end", "etf_code", "net_pnl_nav_units",
                "phase_return_contribution",
            ])
        contributions.to_csv(folder / "phase_contributions.csv", index=False,
                             encoding="utf-8-sig")
        enhanced.to_csv(folder / "daily.csv", index=False, encoding="utf-8-sig")
        exposure.to_csv(folder / "actual_weights.csv", encoding="utf-8-sig")
        pd.DataFrame(result["trades"], columns=[
            "date", "signal_date", "code", "side", "quantity", "price", "fee", "status",
        ]).to_csv(folder / "trades.csv", index=False)
        pd.DataFrame(result["warnings"], columns=["date", "code", "reason"]).to_csv(
            folder / "warnings.csv", index=False)
        write_json(folder / "metrics.json", {"metrics": result["metrics"], "tests": tests,
                                              "diagnostics": diagnostics})
        manifest = {"strategy": name, "config": cfg, "metadata": dataset.metadata,
                    "source_audit": source_audit, "code_hash": source_hash,
                    "requested_start": start, "requested_end": end, "split_date": cut,
                    "etf_names": names, "phase_reconciliation": "passed",
                    "note": "No personal capital or risk limits assumed"}
        write_json(folder / "manifest.json", manifest)
        with gzip.open(folder / "full_result.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(clean(result), handle, ensure_ascii=False, allow_nan=False)
        plot_curves(result, benchmark, results["strategic"], folder, enhanced, exposure)
        render_report(result, phases, candidates, diagnostics, tests, folder, benchmark)
        summary_rows.append({"strategy": name, "name": NAMES[name],
                             "status": result["status"], "phases": len(phases),
                             "first_active_signal": result["first_active_signal"],
                             **result["metrics"], "diagnostics": diagnostics})
    write_json(output / "summary.json", summary_rows)
    index = ["# 五种ETF策略：十年独立回测报告", "", f"请求窗口：{start}—{end}。", "",
             "**全部为原始价格探索研究，不是实盘绩效。第五种未准入时不参与收益排名。**", "",
             md_table(["策略", "CAGR", "最大回撤", "阶段数", "报告"], [
                 [r["name"], percent(r["cagr"]) if r["first_active_signal"] else "不适用",
                  percent(r["max_drawdown"]) if r["first_active_signal"] else "不适用",
                  r["phases"], f"[独立报告]({r['strategy']}/report.md)"] for r in summary_rows]), "",
             "每份报告包含逐调仓期收益、逐ETF候选与排除、净损益贡献、日净值、仓位曲线、"
             "年度表现、成本翻倍与执行延迟测试、评价及改进建议。", ""]
    (output / "README.md").write_text("\n".join(index), encoding="utf-8")
    print(f"Completed: {output.resolve() / 'README.md'}", flush=True)


def main():
    # Fix a reproducible ten-calendar-year interval ending at the explicit local data cutoff.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/security_pool.db")
    parser.add_argument("--start", default="2016-09-12")
    parser.add_argument("--end", default="2026-09-11")
    parser.add_argument("--output", type=Path, default=Path("reports/etf_ten_year_20260913"))
    args = parser.parse_args()
    run(args.output, args.db, args.start, args.end)


if __name__ == "__main__":
    main()
