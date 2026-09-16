"""Frozen A--F experiments, complete screening evidence and publication-safe reports."""

import argparse
import copy
import gzip
import html
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import pandas as pd

from .backtest import backtest, metrics
from .config import LEGACY_STRATEGIES, load_config
from .data import Dataset
from .storage import clean, digest, save_run
from .ten_year import phase_ledger


def experiments():
    # Freeze old reference membership and every ablation without return-based optimization.
    base = ["510300.SH", "513100.SH", "511010.SH", "518880.SH", "159985.SZ"]
    factor = ["510030.SH", "515910.SH", "512260.SH"]
    specs = []
    for strategy in LEGACY_STRATEGIES:
        specs.append(("A_" + strategy, strategy, {"allowed_codes":
                      factor if strategy == "factor_rotation" else base}))
    specs.append(("B_expanded", "dual_momentum", {"universe_pool": "all_multi"}))
    for stage in ("C", "D", "E"):
        for strategy in ("multi_rotation", "equity_rotation"):
            specs.append((stage + "_" + strategy, strategy, {
                "rotation_constraints": stage != "C", "rotation_buffer": stage == "E"}))
    specs.append(("reference_CSI300", "buy_hold", {"allowed_codes": ["510300.SH"]}))
    return [(name + suffix, strategy, load_config(overrides={
        "version": "2.0.0", "price_mode": "verified_prices",
        **settings, "target_volatility": vol}))
        for name, strategy, settings in specs
        for suffix, vol in (("_base", None), ("_vol10", .10))]


def write_json(path, value):
    # Persist reproducible strict JSON, including blocked states rather than invented returns.
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def scan_queue(dataset, day):
    # Scan all source products; unmatched names remain suggestions, never verified classifications.
    frame = dataset.universe(day, "all_multi")
    master = dataset.master.set_index("etf_code")
    result = frame.copy()
    result["underlying_code"] = result.etf_code.map(master.underlying_code)
    result["factor_observations"] = result.etf_code.map({
        c: int(b.forward_factor.gt(0).sum()) for c, b in dataset.bars.items()})
    result["unexplained_actions"] = result.etf_code.map({
        c: len(v) for c, v in dataset.action_issues.items()})
    result["review_priority"] = result.adv20_10k.fillna(0)
    return result.sort_values(["review_priority", "etf_code"], ascending=[False, True])


def segments(run):
    # Normalize at the prior close to retain the first return in each 60/40 segment.
    if run["status"] != "evaluated":
        return {}
    rows = run["curve"]
    boundary = int(len(rows) * .6)
    output = {}
    for label, first, last in (("development", 0, boundary),
                                ("validation", boundary, len(rows))):
        anchor = rows[first - 1]["nav"] if first else 1
        part = [{**r, "nav": r["nav"] / anchor} for r in rows[first:last]]
        output[label] = metrics(part)
    return output


def report_run(folder, name, run, dataset, tests):
    # Export every phase and signal; suppress investment curves for blocked or inactive studies.
    folder.mkdir(parents=True, exist_ok=True)
    compact = {**run, "signals": [{k: v for k, v in s.items() if k != "universe"}
                                   for s in run["signals"]],
               "universe_artifact": "all_screening.csv.gz"}
    with gzip.open(folder / "result.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(clean(compact), stream, ensure_ascii=False, allow_nan=False)
    write_json(folder / "config.json", dataset.config)
    write_json(folder / "stress_and_segments.json", tests)
    pd.DataFrame(run["trades"]).to_csv(folder / "trades.csv", index=False)
    screening, ranking, steps = [], [], []
    for decision in run["signals"]:
        day = decision["signal_date"]
        rows = decision.get("universe", [])
        screening.extend({**r, "signal_date": day} for r in rows)
        for rank in decision.get("diagnostics", {}).get("ranking", []):
            ranking.append({"signal_date": day, **rank})
        reasons = sorted({reason for r in rows for reason in r.get("reasons", [])})
        remaining = list(rows)
        pipeline = {}
        gates = {
            "identity": ("not_listed_or_unknown_listing", "historical_identity_missing",
                         "historical_identity_invalid", "outside_frozen_reference_pool"),
            "classification": ("classification_unverified_or_wrong_pool",
                               "exposure_type_unverified", "theme_group_unverified",
                               "asset_structure_unverified"),
            "prices": ("adjustment_or_action_unverified", "calendar_unverified"),
            "history": ("no_history", "insufficient_history", "poor_coverage",
                        "invalid_signal_day", "insufficient_recent_trading"),
            "liquidity": ("unknown_liquidity", "low_liquidity"),
            "dedup": ("duplicate_exposure",),
        }
        for stage, failures in gates.items():
            remaining = [r for r in remaining if not set(r.get("reasons", [])) & set(failures)]
            pipeline["after_" + stage] = len(remaining)
        steps.append({"signal_date": day, "scanned": len(rows),
                      "eligible": sum(r["eligible"] for r in rows),
                      "status": decision["status"], "reason": decision.get("reason"),
                      **pipeline,
                      **{r: sum(r in x.get("reasons", []) for x in rows) for r in reasons}})
    pd.DataFrame(screening).to_csv(folder / "all_screening.csv.gz", index=False,
                                   compression="gzip")
    pd.DataFrame(ranking).to_csv(folder / "ranking.csv", index=False)
    pd.DataFrame(steps).to_csv(folder / "screening_steps.csv", index=False)
    write_json(folder / "decisions.json", [
        {k: v for k, v in r.items() if k != "universe"} for r in run["signals"]])
    lines = [f"# {name}", "", f"状态：{run['status']}。", "",
             "存续样本研究；核验合格者参与，不代表完整历史ETF名单。",
             "10%为事前研究目标，只减仓；实际波动率不保证等于10%。", ""]
    if run["status"] == "evaluated":
        frame = pd.DataFrame(run["curve"])
        frame.to_csv(folder / "daily.csv", index=False)
        phases, contribution = phase_ledger(run, dataset)
        phases.to_csv(folder / "phases.csv", index=False)
        contribution.to_csv(folder / "contributions.csv", index=False)
        if (phases.reconciliation_residual.abs() > 1e-8).any():
            raise ValueError("Phase attribution does not reconcile")
        plt.rcParams.update({"font.family": ["Microsoft YaHei", "DejaVu Sans"],
                             "axes.unicode_minus": False})
        fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
        x = pd.to_datetime(frame.date)
        axes[0].plot(x, frame.nav - 1)
        axes[0].set_title(name + "｜已核验价格、存续样本研究")
        axes[1].fill_between(x, frame.nav / frame.nav.cummax().clip(lower=1) - 1, 0)
        weights = pd.DataFrame([{
            **{c: v / r["nav"] for c, v in r["holding_values"].items()},
            "CASH": r["cash_weight"], "RECEIVABLE": r["receivables"] / r["nav"]}
            for r in run["curve"]]).fillna(0)
        weights.insert(0, "date", frame.date)
        weights.to_csv(folder / "actual_weights.csv", index=False)
        allocation = weights.drop(columns="date")
        axes[2].stackplot(x, *[allocation[c] for c in allocation], labels=allocation.columns)
        axes[2].legend(fontsize=7, ncol=4)
        for axis, label in zip(axes, ("累计收益", "回撤", "实际仓位")):
            axis.set_ylabel(label)
            axis.yaxis.set_major_formatter(PercentFormatter(1))
            axis.grid(alpha=.2)
        fig.tight_layout()
        fig.savefig(folder / "returns.png", dpi=140)
        plt.close(fig)
        metric = run["metrics"]
        lines += [f"年化 {metric['cagr']:.2%}；最大回撤 {metric['max_drawdown']:.2%}；",
                  f"实际波动率 {metric['realized_volatility']:.2%}；"
                  f"平均现金 {metric['average_cash_weight']:.2%}。", "",
                  "![收益与仓位曲线](returns.png)", "",
                  "评价：需结合共同有效区间、换手与成本压力结果比较，不能只按年化排序。",
                  "改进方向：先检查收益是否依赖少数主题和年份，再考虑新的独立实验。"]
        lines += ["", f"Sharpe（零无风险）：{metric['sharpe_zero_rf']}；"
                  f"Calmar：{metric['calmar']}。",
                  f"最长回撤：{metric['longest_drawdown_sessions']}个交易日；"
                  f"尚未修复：{metric['ongoing_drawdown_sessions']}个交易日。",
                  f"累计单边换手：{metric['one_way_turnover']:.2f}。", "", "年度收益："]
        lines += [f"- {year}：{value:.2%}" for year, value in metric["annual_returns"].items()]
    else:
        lines += ["**不发布年化、回撤或现金零收益曲线，不计入策略排名。**", "",
                  "当前没有可发布的合格活跃区间，或持仓出现未解释公司行动。",
                  "逐期缺口见 screening_steps.csv，完整ETF名单见 all_screening.csv.gz。",
                  "改进顺序：补齐分类及历史收益证据，保持流动性门槛与参数不变后重跑。"]
    lines += ["", "其他思考：扩大候选池也可能增加集中风险和过拟合机会。",
              "历史前60%/后40%划分并非真正未见数据，实施后新数据另作前瞻观察。",
              "交易使用下一交易日原始开盘价、费用3bp、滑点5bp；不含个人账户整手影响。"]
    text = "\n".join(lines)
    (folder / "report.md").write_text(text, encoding="utf-8")
    body = "<meta charset='utf-8'><style>body{max-width:1100px;margin:40px auto;"
    body += "font:16px/1.8 sans-serif}pre{white-space:pre-wrap}img{width:100%}</style>"
    body += "<pre>" + html.escape(text) + "</pre>"
    if run["status"] == "evaluated":
        body += "<img src='returns.png'>"
    (folder / "report.html").write_text(body, encoding="utf-8")


def run_suite(path, output, start="2016-09-12", end="2026-09-11", results_db="data/etf_strategy.db"):
    # Recompute stateful decisions separately under each cost/delay scenario.
    output.mkdir(parents=True, exist_ok=True)
    data = Dataset(path, load_config(overrides={"price_mode": "verified_prices"}), end)
    scan_queue(data, end).to_csv(output / "verification_queue.csv", index=False)
    write_json(output / "action_issues.json", data.action_issues)
    summary, runs = {}, {}
    for name, strategy, config in experiments():
        print(f"Running {name}", flush=True)
        data.config = config
        result = backtest(data, strategy, start, end)
        tests = {"historical_segments": segments(result)}
        if result["status"] == "evaluated":
            for label in ("double_cost", "one_session_delay"):
                changed = copy.deepcopy(config)
                if label == "double_cost":
                    changed["fee_bps"] *= 2
                    changed["slippage_bps"] *= 2
                else:
                    changed["execution_delay"] = 1
                data.config = changed
                stress = backtest(data, strategy, start, end)
                tests[label] = {"status": stress["status"], "metrics": stress["metrics"]}
        data.config = config
        report_run(output / name, name, result, data, tests)
        run_id, _ = save_run(results_db, "full-market:" + name, config, {
            "metadata": result["metadata"], "status": result["status"],
            "metrics": result["metrics"], "artifact": str((output / name).resolve()),
            "artifact_digest": hashlib.sha256(
                gzip.decompress((output / name / "result.json.gz").read_bytes())).hexdigest(),
            "tests": tests})
        summary[name] = {"status": result["status"], "metrics": result["metrics"]
                         if result["status"] == "evaluated" else None, "run_id": run_id}
        runs[name] = (strategy, config, result["first_active_signal"], result["status"])
    active = {n: r for n, r in runs.items() if r[3] == "evaluated"}
    common = max((r[2] for r in active.values()), default=None)
    for name, (strategy, config, _, _) in active.items():
        data.config = config
        fresh = backtest(data, strategy, common, end)
        summary[name]["common_metrics"] = fresh["metrics"]
        summary[name]["common_status"] = fresh["status"]
    write_json(output / "summary.json", {"start": start, "end": end,
               "common_start": common, "experiments": summary,
               "F_aliases": ["E_multi_rotation_vol10", "E_equity_rotation_vol10"]})
    lines = ["# 全市场ETF轮动研究", "", f"请求窗口：{start}—{end}。", "",
             "全量扫描、合格者准入；未核验结果不发布收益。", "",
             "|实验|状态|报告|", "|---|---|---|"]
    lines += [f"|{n}|{r['status']}|[独立报告]({n}/report.md)|" for n, r in summary.items()]
    lines += ["", "F直接引用E的10%版本，不重复运行。",
              "verification_queue.csv列出全量核验队列；action_issues.json列出异常价格日期。",
              "共同区间与对照结果见summary.json；暂无合格结果时不宣称收益提高。"]
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main():
    # Provide a reproducible standalone entrypoint alongside the existing strategy CLI.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/security_pool.db")
    parser.add_argument("--results-db", default="data/etf_strategy.db")
    parser.add_argument("--output", type=Path, default=Path("reports/etf_full_market"))
    parser.add_argument("--start", default="2016-09-12")
    parser.add_argument("--end", default="2026-09-11")
    args = parser.parse_args()
    run_suite(args.db, args.output, args.start, args.end, args.results_db)


if __name__ == "__main__":
    main()
