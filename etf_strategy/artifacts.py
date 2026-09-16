"""Small reviewable summaries alongside complete reproducibility artifacts."""

import json

import pandas as pd


def export_artifacts(output, result):
    # Export compact comparison tables and ledgers without requiring readers to open giant JSON.
    lines = ["# ETF 研究运行结果", "", "完整输入、证据和参数见同目录 JSON。", ""]
    if "results" in result:
        lines += ["**探索性研究，不是实盘绩效或收益预测。**", "",
                  "|策略|状态|CAGR|最大回撤|Sharpe|成交笔数|",
                  "|---|---|---:|---:|---:|---:|"]
        summaries = []
        for name, run in result["results"].items():
            metric = run["metrics"]
            active = run["status"] == "evaluated"
            cagr = f"{metric['cagr']:.2%}" if active else "不适用"
            drawdown = f"{metric['max_drawdown']:.2%}" if active else "不适用"
            sharpe = metric.get("sharpe_zero_rf")
            sharpe_text = f"{sharpe:.3f}" if sharpe is not None and active else "不适用"
            lines.append(f"|{name}|{run['status']}|{cagr}|{drawdown}|{sharpe_text}|"
                         f"{len(run['trades'])}|")
            summaries.append({"strategy": name, "status": run["status"], **metric})
            folder = output / name
            folder.mkdir(exist_ok=True)
            pd.DataFrame(run["curve"]).to_csv(folder / "nav.csv", index=False)
            pd.DataFrame(run["trades"]).to_csv(folder / "trades.csv", index=False)
            signal_rows = [{k: v for k, v in row.items() if k != "universe"}
                           for row in run["signals"]]
            (folder / "signals.json").write_text(
                json.dumps(signal_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        pd.DataFrame(summaries).to_csv(output / "comparison.csv", index=False,
                                       encoding="utf-8-sig")
        lines += ["", f"共同重新起算日：{result['common_start']}。",
                  f"历史开发/验证分界：{result['split_date']}；不代表真正未见数据。", "",
                  "当前分类种子为小规模核验池，存在生存者偏差；原始价格未补齐分红事件。",
                  "第五种策略无合格活跃区间时不以现金零收益参与排名。"]
        summary = {k: v for k, v in result.items() if k != "results"}
        summary["strategies"] = summaries
        (output / "comparison_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    elif "curve" in result:
        pd.DataFrame(result["curve"]).to_csv(output / "nav.csv", index=False)
        pd.DataFrame(result["trades"]).to_csv(output / "trades.csv", index=False)
        lines += [f"质量等级：{result['quality_level']}", "",
                  f"策略：{result['strategy']}；状态：{result['status']}"]
    else:
        lines += [f"状态：{result.get('status', result.get('quality_level', '见完整结果'))}"]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
