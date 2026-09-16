"""Reconcile published report tables and bundle independently readable artifacts."""

import argparse
import json
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import STRATEGIES
from .ten_year import NAMES, write_json


def verify(root):
    # Validate all phase partitions, contribution ledgers, screening snapshots and chart assets.
    checks = {}
    plt.rcParams["font.family"] = ["Microsoft YaHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, axis = plt.subplots(figsize=(13, 6))
    for name in STRATEGIES:
        folder = root / name
        # Empty ledgers retain column headers so spreadsheet tools can read non-eligible strategies.
        schemas = {
            "trades.csv": ["date", "signal_date", "code", "side", "quantity", "price",
                           "fee", "status"],
            "warnings.csv": ["date", "code", "reason"],
            "phase_contributions.csv": ["phase", "signal_date", "start", "end", "etf_code",
                                        "net_pnl_nav_units", "phase_return_contribution"],
        }
        for filename, columns in schemas.items():
            if (folder / filename).stat().st_size <= 5:
                pd.DataFrame(columns=columns).to_csv(folder / filename, index=False)
        daily = pd.read_csv(folder / "daily.csv")
        phases = pd.read_csv(folder / "phases.csv")
        contributions = pd.read_csv(folder / "phase_contributions.csv")
        exposures = pd.read_csv(folder / "actual_weights.csv").set_index("date")
        trades = pd.read_csv(folder / "trades.csv")
        metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))["metrics"]
        screening = pd.read_csv(folder / "all_screening.csv.gz", usecols=["phase", "etf_code"])
        assert not screening.duplicated(["phase", "etf_code"]).any()
        assert phases.sessions.sum() == len(daily)
        assert np.isclose((1 + phases.phase_return).prod(), daily.nav.iloc[-1])
        assert np.isclose(np.prod([1 + r for r in metrics["annual_returns"].values()]),
                          daily.nav.iloc[-1])
        assert np.max(np.abs(exposures.sum(axis=1) - 1)) < 1e-7
        seen = []
        contribution_totals = contributions.groupby("phase").phase_return_contribution.sum()
        for row in phases.itertuples():
            part = daily[daily.date.ge(row.start) & daily.date.le(row.end)]
            seen.extend(part.date)
            assert len(part) == row.sessions
            assert np.isclose(part.nav.iloc[-1] / row.start_nav - 1, row.phase_return)
            assert np.isclose(contribution_totals.get(row.phase, 0), row.phase_return, atol=1e-9)
        assert seen == daily.date.tolist()
        assert (trades.date > trades.signal_date).all()
        assert np.isclose(daily.drawdown.min(), metrics["max_drawdown"])
        for filename in ("report.md", "report.html", "returns.png", "returns.svg"):
            assert (folder / filename).stat().st_size > 100
        inactive = len(trades) == 0
        axis.plot(pd.to_datetime(daily.date), (daily.nav - 1) * 100,
                  label=NAMES[name] if not inactive else "纯风格策略：未准入现金线（非业绩）",
                  linestyle="--" if inactive else "-", linewidth=1.8)
        checks[name] = {"status": "passed", "trading_sessions": len(daily),
                        "phases": len(phases), "screening_records": len(screening),
                        "trades": len(trades), "phase_and_asset_pnl_reconciled": True}
    axis.set_title("五种ETF策略｜2016-09-12 至 2026-09-11", loc="left", fontsize=16, pad=16)
    axis.set_ylabel("累计收益（%）")
    axis.grid(alpha=.2)
    axis.legend(loc="upper left", fontsize=9)
    axis.spines[["top", "right"]].set_visible(False)
    fig.text(.08, .025, "原价探索研究，未完整计入分红；存续偏差；第五种未准入，不参加绩效排名。",
             fontsize=10, color="#795548")
    fig.tight_layout(rect=(0, .05, 1, 1))
    fig.savefig(root / "comparison.png", dpi=160)
    plt.close(fig)
    write_json(root / "report_verification.json", checks)
    index = root / "README.md"
    current = index.read_text(encoding="utf-8")
    if "comparison.png" not in current:
        current += "\n![五策略累计收益对照](comparison.png)\n"
        current += "\n逐阶段、单资产损益、年度收益、每日权重与交易时间校验均通过。"
        current += "详细证据见 [报告核验记录](report_verification.json)。\n"
        index.write_text(current, encoding="utf-8")
    return checks


def bundle(root):
    # Package reports and all independent CSV/figure evidence without recursively including itself.
    archive = root.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for file in sorted(root.rglob("*")):
            if file.is_file():
                handle.write(file, arcname=str(file.relative_to(root.parent)))
    return archive


def main():
    # Verify the concrete generated reports before producing the distribution archive.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    checks = verify(args.root)
    archive = bundle(args.root)
    print(json.dumps({"checks": checks, "archive": str(archive.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
