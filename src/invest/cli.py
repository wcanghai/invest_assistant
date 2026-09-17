"""统一命令入口，将既有稳定业务实现纳入新的运行边界。"""

from __future__ import annotations

import argparse
import json
import runpy
import sqlite3
import sys
from contextlib import closing
from contextlib import nullcontext
from pathlib import Path

from invest.core.settings import load_settings
from invest.storage.migration import check_sources
from invest.storage.migration import migrate
from invest.storage.migration import verify


def _delegate(module: str, arguments: list[str]) -> None:
    # 以模块方式调用既有业务入口，保留原有参数和算法行为。
    previous = sys.argv
    try:
        from invest.core.runs import research_run
        from invest.core.locking import file_lock
        from invest.core.runs import option

        arguments = _absolute_paths(arguments)
        recording = module.startswith("invest.research.") and not any(
            item in ("--help", "-h", "init-db", "collect", "migrate", "import-data",
                     "import-account", "import-fills", "import-account-events", "reconcile",
                     "trade-plan") for item in arguments)
        recording = recording and not module.endswith(".verify_reports")
        context = research_run(module, arguments) if recording else nullcontext(arguments)
        writing_market = module.startswith("invest.market.") or any(
            item in ("collect", "migrate", "import-data") for item in arguments
        ) and module == "invest.research.etf"
        writing_market = writing_market and not module.endswith("daily_helper") and not any(
            item in ("--help", "-h") for item in arguments)
        database = option(arguments, "--db", str(load_settings().databases["market"]))
        lock = file_lock(Path(database).resolve().with_suffix(".update.lock"))
        with lock if writing_market else nullcontext():
            with context as forwarded:
                sys.argv = [module, *forwarded]
                runpy.run_module(module, run_name="__main__")
    finally:
        sys.argv = previous


def _absolute_paths(arguments: list[str]) -> list[str]:
    # 用户显式相对文件路径以调用目录解析；后续日志和研究档案均记录绝对路径。
    names = {"--db", "--market-db", "--results-db", "--research-db", "--source-db",
             "--report-db", "--config", "--output", "--output-dir", "--input", "--account",
             "--holdings", "--execution"}
    result = list(arguments)
    for index, item in enumerate(result):
        name, separator, value = item.partition("=")
        if name not in names:
            continue
        if separator:
            result[index] = name + "=" + str(Path(value).resolve())
        elif index + 1 < len(result) and not result[index + 1].startswith("--"):
            result[index + 1] = str(Path(result[index + 1]).resolve())
    return result


def _parser() -> argparse.ArgumentParser:
    # 建立统一命令分组与透传参数结构。
    parser = argparse.ArgumentParser(description="Invest 本地量化平台")
    sub = parser.add_subparsers(dest="group", required=True)
    db = sub.add_parser("db", help="数据库检查、迁移与核对")
    db.add_argument("action", choices=("check", "migrate", "verify"))
    market = sub.add_parser("market", help="市场数据")
    market.add_argument("area", choices=("pool", "stock", "etf", "helper", "financial"))
    market.add_argument("arguments", nargs=argparse.REMAINDER)
    research = sub.add_parser("research", help="策略研究")
    research.add_argument(
        "area",
        choices=("factor", "etf", "mvp", "three-momentum", "full-market", "ten-year",
                 "verify-reports"),
    )
    research.add_argument("arguments", nargs=argparse.REMAINDER)
    accounts = sub.add_parser("accounts", help="ETF 账户和对账")
    accounts.add_argument("arguments", nargs=argparse.REMAINDER)
    report = sub.add_parser("report", help="投资日报和本地网站")
    report.add_argument("area", choices=("run", "web"))
    report.add_argument("arguments", nargs=argparse.REMAINDER)
    jobs = sub.add_parser("jobs", help="定时作业")
    jobs.add_argument("area", choices=("report", "market", "status"))
    jobs.add_argument("arguments", nargs=argparse.REMAINDER)
    doctor = sub.add_parser("doctor", help="检查本机运行配置")
    doctor.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _append_default(arguments: list[str], option: str, value: Path) -> list[str]:
    # 用户未传入指定参数时附加配置中的默认绝对路径。
    supplied = any(arg == option or arg.startswith(option + "=") for arg in arguments)
    return arguments if supplied else [*arguments, option, str(value)]


def _doctor() -> dict[str, object]:
    # 返回数据库、配置和 Python 环境的可读状态。
    settings = load_settings()
    local = settings.root / "config.local.toml"
    return {
        "root": str(settings.root),
        "local_config": str(local) if local.exists() else None,
        "databases": {name: _database_status(path)
                      for name, path in settings.databases.items()},
        "python": sys.executable,
    }


def _database_status(path: Path) -> dict[str, object]:
    # 只读检查数据库是否为空或缺少表，避免把空文件误报为可用。
    result = {"path": str(path), "exists": path.exists(), "tables": []}
    if not path.is_file() or not path.stat().st_size:
        result["status"] = "missing_or_empty"
        return result
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            result["tables"] = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
        result["status"] = "readable" if result["tables"] else "empty"
    except sqlite3.Error as error:
        result.update(status="error", error=str(error))
    return result


def main() -> None:
    # 分派统一命令，并只在需要时加载既有业务模块。
    args = _parser().parse_args()
    settings = load_settings()
    if args.group == "db":
        action = {"check": check_sources, "migrate": migrate, "verify": verify}[args.action]
        print(json.dumps(action(settings), ensure_ascii=False, indent=2))
        return
    if args.group == "doctor":
        print(json.dumps(_doctor(), ensure_ascii=False, indent=2))
        return
    if args.group == "market":
        module = {"pool": "invest.market.security_pool.main", "stock": "invest.market.stock.main",
                  "etf": "invest.market.security_pool.etf_data",
                  "helper": "invest.market.stock.daily_helper",
                  "financial": "invest.market.stock.financial_delta"}[args.area]
        if args.area == "helper":
            _delegate(module, args.arguments)
            return
        _delegate(module, _append_default(args.arguments, "--db", settings.databases["market"]))
        return
    if args.group == "research":
        if args.area == "verify-reports":
            _delegate("invest.research.etf.verify_reports", args.arguments)
            return
        if args.area == "factor":
            forwarded = _append_default(args.arguments, "--db", settings.databases["research"])
            forwarded = _append_default(forwarded, "--market-db", settings.databases["market"])
            forwarded = _append_default(forwarded, "--output",
                settings.path(settings.config['paths']['report_dir']) / 'factor')
            _delegate("invest.research.stocks.main", forwarded)
            return
        if args.area == "mvp":
            forwarded = _append_default(args.arguments, "--db", settings.databases["market"])
            forwarded = _append_default(forwarded, "--output-dir",
                settings.path(settings.config['paths']['report_dir']) / 'mvp')
            _delegate("invest.research.stocks.mvp", forwarded)
            return
        if args.area == "three-momentum":
            forwarded = _append_default(args.arguments, "--db", settings.databases["market"])
            forwarded = _append_default(
                forwarded, "--config", settings.root / 'configs/etf_three_momentum.json'
            )
            forwarded = _append_default(
                forwarded, "--output",
                settings.path(settings.config['paths']['report_dir']) / 'three-momentum'
            )
            _delegate("invest.research.etf.three_momentum", forwarded)
            return
        module = {"etf": "invest.research.etf", "full-market": "invest.research.etf.full_market",
                  "ten-year": "invest.research.etf.ten_year"}[args.area]
        forwarded = _append_default(args.arguments, "--db", settings.databases["market"])
        forwarded = _append_default(
            forwarded, "--output", settings.path(settings.config['paths']['report_dir']) / args.area
        )
        if args.area == "etf":
            forwarded = _append_default(forwarded, "--results-db", settings.databases["research"])
        if args.area == "full-market":
            forwarded = _append_default(forwarded, "--results-db", settings.databases["research"])
        _delegate(module, forwarded)
        return
    if args.group == "accounts":
        forwarded = _append_default(args.arguments, "--results-db", settings.databases["accounts"])
        forwarded = _append_default(forwarded, "--db", settings.databases["market"])
        forwarded = _append_default(forwarded, "--research-db", settings.databases["research"])
        forwarded = _append_default(forwarded, "--output",
            settings.path(settings.config['paths']['report_dir']) / 'accounts')
        _delegate("invest.research.etf", forwarded)
        return
    if args.group == "report":
        if args.area == "web":
            _delegate("invest.reporting.web", args.arguments)
            return
        forwarded = _append_default(args.arguments, "--source-db", settings.databases["market"])
        forwarded = _append_default(forwarded, "--report-db", settings.databases["reports"])
        forwarded = _append_default(forwarded, "--output",
            settings.path(settings.config['paths']['report_dir']) / 'daily')
        _delegate("invest.reporting.cli", forwarded)
        return
    if args.group == "jobs":
        _delegate("invest.jobs." + args.area, args.arguments)
