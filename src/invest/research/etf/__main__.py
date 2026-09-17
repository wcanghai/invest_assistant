"""通过 invest research etf 或 invest accounts 运行本地工作流。"""

import argparse
import copy
import json
from pathlib import Path
from invest.core.settings import load_settings

from invest.research.etf.backtest import backtest
from invest.research.etf.backtest import metrics
from invest.research.etf.backtest import rebalance_days
from invest.research.etf.artifacts import export_artifacts
from invest.market.etf_collect import collect
from invest.research.etf.config import LEGACY_STRATEGIES
from invest.research.etf.config import STRATEGIES
from invest.research.etf.config import load_config
from invest.research.etf.data import Dataset
from invest.research.etf.data import audit
from invest.accounts.manual import import_account_events
from invest.accounts.manual import import_fills
from invest.accounts.manual import import_snapshot
from invest.accounts.manual import reconcile
from invest.accounts.manual import trade_plan
from invest.storage.etf import clean
from invest.storage.etf import import_records
from invest.storage.etf import migrate
from invest.storage.etf import save_run
from invest.research.etf.strategies import target
from invest.research.etf.validation import validate_prices


def read_json(path):
    # Load an explicitly supplied JSON input, leaving missing optional inputs unset.
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def compare(dataset, start, end, account=None):
    # Reuse frozen signals for cost stress and report separate and common active intervals.
    results, stress = {}, {}
    cached = {name: {} for name in LEGACY_STRATEGIES}
    monthly = rebalance_days(dataset, "trend")
    quarterly = rebalance_days(dataset, "strategic")
    for day in sorted(monthly):
        if not start <= day <= end:
            continue
        base = dataset.universe(day)
        factor = dataset.universe(day, "factor")
        for name in LEGACY_STRATEGIES:
            if name != "strategic" or day in quarterly:
                cached[name][day] = target(dataset, day, name,
                                           factor if name == "factor_rotation" else base)
    for name in LEGACY_STRATEGIES:
        print(f"Backtesting {name}...", flush=True)
        result = backtest(dataset, name, start, end, account, cached[name])
        results[name] = result
        old = dataset.config
        expensive = copy.deepcopy(old)
        expensive["fee_bps"] *= 2
        expensive["slippage_bps"] *= 2
        dataset.config = expensive
        stressed_account = copy.deepcopy(account)
        if stressed_account:
            for key in ("fee_bps", "minimum_fee", "slippage_bps"):
                stressed_account[key] *= 2
        try:
            stressed = backtest(dataset, name, start, end, stressed_account, cached[name])
            stress[name] = stressed["metrics"]
        finally:
            dataset.config = old
    active = {n: r for n, r in results.items() if r["first_active_signal"]}
    common_start = max((r["first_active_signal"] for r in active.values()), default=None)
    common = {}
    if common_start:
        # Rerun from the common signal date with fresh cash instead of renormalizing old holdings.
        for name in active:
            common[name] = backtest(dataset, name, common_start, end, account,
                                    cached[name])["metrics"]
    dates = [d for d in dataset.dates if start <= d <= end]
    split = dates[int(len(dates) * 0.6)] if dates else None
    segments = {}
    for name, result in results.items():
        segments[name] = {}
        for label, rows in (
            ("development", [r for r in result["curve"] if r["date"] < split]),
            ("validation", [r for r in result["curve"] if r["date"] >= split]),
        ):
            if rows:
                prior = [r for r in result["curve"] if r["date"] < rows[0]["date"]]
                anchor = prior[-1]["nav"] if prior else 1.0
                normalized = [{**r, "nav": r["nav"] / anchor} for r in rows]
                segments[name][label] = metrics(normalized)
    return {"results": results, "double_cost_metrics": stress, "common_start": common_start,
            "common_metrics": common, "split_date": split, "segments": segments,
            "disclosure": "Historical 60/40 split is not unseen prospective evidence"}


def parser():
    # Expose research, explicit supplementation and manual bookkeeping as separate commands.
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=("audit", "migrate", "import-data", "collect",
                                         "validate-prices", "full-market",
                                         "universe", "signal", "backtest", "compare",
                                         "trade-plan", "import-account", "import-fills",
                                         "reconcile", "import-account-events"))
    cli.add_argument("--db", default=str(load_settings().databases["market"]))
    cli.add_argument("--results-db", default=str(load_settings().databases["research"]))
    cli.add_argument("--research-db", help="独立研究运行记录库；账户命令使用")
    cli.add_argument("--config", type=Path)
    cli.add_argument("--date")
    cli.add_argument("--start")
    cli.add_argument("--end")
    cli.add_argument("--strategy", choices=STRATEGIES, default="dual_momentum")
    cli.add_argument("--pool", choices=("base", "factor", "all_multi", "all_equity"),
                     default="base")
    cli.add_argument("--input")
    cli.add_argument("--table")
    cli.add_argument("--codes", nargs="+")
    cli.add_argument("--account")
    cli.add_argument("--account-id")
    cli.add_argument("--holdings", help="JSON mapping of actual code to quantity")
    cli.add_argument("--execution")
    cli.add_argument("--output", type=Path, default=Path("reports/etf_strategy"))
    return cli


def main():
    # Dispatch work and persist full artifacts while printing only a compact run summary.
    args = parser().parse_args()
    cfg = load_config(args.config)
    command = args.command
    args.start = args.start or ("2016-09-12" if command == "full-market"
                               or cfg["version"] == "2.0.0" else "2020-01-01")
    if command == "full-market" or (command == "compare" and cfg["version"] == "2.0.0"):
        from invest.research.etf.full_market import run_suite

        run_suite(args.db, args.output, args.start, args.end or "2026-09-11", args.results_db)
        return
    if command == "migrate":
        migrate(args.db)
        result = {"status": "migrated"}
    elif command == "import-data":
        if not args.input or not args.table:
            raise ValueError("--input and --table are required")
        result = {"imported": import_records(args.db, args.table, read_json(args.input))}
    elif command == "collect":
        if not args.codes or not args.end:
            raise ValueError("--codes and --end required for bounded supplementation")
        result = collect(args.db, args.codes, args.start, args.end,
                         args.output / "vendor_evidence.json")
    elif command == "audit":
        result = audit(args.db)
    elif command == "validate-prices":
        if not args.codes:
            raise ValueError("--codes required")
        result = validate_prices(args.db, args.codes)
    elif command == "import-account":
        result = {"status": import_snapshot(args.results_db, read_json(args.input))}
    elif command == "import-fills":
        result = import_fills(args.results_db, args.account_id, read_json(args.input))
    elif command == "import-account-events":
        result = import_account_events(args.results_db, args.account_id, read_json(args.input))
    elif command == "reconcile":
        result = reconcile(args.results_db, read_json(args.input))
    else:
        dataset = Dataset(args.db, cfg, args.end or args.date)
        day = args.date or dataset.end
        if command == "universe":
            frame = dataset.universe(day, args.pool)
            result = {"metadata": dataset.metadata, "date": day,
                      "eligible_count": int(frame.eligible.sum()),
                      "universe": frame.to_dict("records")}
        elif command == "signal":
            result = target(dataset, day, args.strategy, holdings=read_json(args.holdings))
            result["metadata"] = dataset.metadata
        elif command == "trade-plan":
            snapshot = read_json(args.account)
            holdings = read_json(args.holdings)
            if snapshot:
                holdings = snapshot.get("holdings", holdings)
            decision = target(dataset, day, args.strategy, holdings=holdings)
            if snapshot and not reconcile(args.results_db, snapshot)["matched"]:
                raise ValueError("Broker account differs from ledger; reconcile before planning")
            result = trade_plan(dataset, decision, snapshot, read_json(args.execution))
        elif command == "backtest":
            result = backtest(dataset, args.strategy, args.start, dataset.end,
                              read_json(args.account))
        else:
            result = compare(dataset, args.start, dataset.end, read_json(args.account))
    run_id, result = save_run(args.research_db or args.results_db, command, cfg, result)
    output = args.output / run_id[:16]
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (output / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    export_artifacts(output, result)
    if "universe" in result:
        import pandas as pd

        pd.DataFrame(result["universe"]).to_csv(output / "universe.csv", index=False,
                                               encoding="utf-8-sig")
    if "orders" in result:
        import pandas as pd

        pd.DataFrame(result["orders"]).to_csv(output / "orders.csv", index=False,
                                             encoding="utf-8-sig")
    print(json.dumps(clean({"run_id": run_id, "output": str(output.resolve()),
                            "summary": result.get("metrics", result.get("status")),
                            "eligible_count": result.get("eligible_count")}),
                     ensure_ascii=True))


if __name__ == "__main__":
    main()
