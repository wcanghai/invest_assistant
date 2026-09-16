"""统一命令入口，将既有稳定业务实现纳入新的运行边界。"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path

from invest.core.settings import load_settings
from invest.storage.migration import check_sources, migrate, verify


def _delegate(module: str, arguments: list[str]) -> None:
    # 以模块方式调用既有业务入口，保留原有参数和算法行为。
    previous = sys.argv
    try:
        sys.argv = [module, *arguments]
        runpy.run_module(module, run_name="__main__")
    finally:
        sys.argv = previous


def _parser() -> argparse.ArgumentParser:
    # 建立统一命令分组与透传参数结构。
    parser = argparse.ArgumentParser(description="Invest 本地量化平台")
    sub = parser.add_subparsers(dest="group", required=True)
    db = sub.add_parser("db", help="数据库检查、迁移与核对")
    db.add_argument("action", choices=("check", "migrate", "verify"))
    market = sub.add_parser("market", help="市场数据")
    market.add_argument("area", choices=("pool", "stock", "etf"))
    market.add_argument("arguments", nargs=argparse.REMAINDER)
    research = sub.add_parser("research", help="策略研究")
    research.add_argument(
        "area",
        choices=("factor", "etf", "mvp", "three-momentum", "full-market", "ten-year"),
    )
    research.add_argument("arguments", nargs=argparse.REMAINDER)
    accounts = sub.add_parser("accounts", help="ETF 账户和对账")
    accounts.add_argument("arguments", nargs=argparse.REMAINDER)
    report = sub.add_parser("report", help="投资日报和本地网站")
    report.add_argument("area", choices=("run", "web"))
    report.add_argument("arguments", nargs=argparse.REMAINDER)
    jobs = sub.add_parser("jobs", help="定时作业")
    jobs.add_argument("area", choices=("report",))
    jobs.add_argument("arguments", nargs=argparse.REMAINDER)
    doctor = sub.add_parser("doctor", help="检查本机运行配置")
    doctor.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _append_default(arguments: list[str], option: str, value: Path) -> list[str]:
    # 用户未传入指定参数时附加配置中的默认绝对路径。
    return arguments if option in arguments else [*arguments, option, str(value)]


def _doctor() -> dict[str, object]:
    # 返回数据库、配置和 Python 环境的可读状态。
    settings = load_settings()
    local = settings.root / "config.local.toml"
    return {
        "root": str(settings.root),
        "local_config": str(local) if local.exists() else None,
        "databases": {name: {"path": str(path), "exists": path.exists()}
                      for name, path in settings.databases.items()},
        "python": sys.executable,
    }


def _set_runtime(settings) -> None:
    # 向尚未迁移的日报适配实现传递新数据库和状态目录。
    os.environ["INVEST_MARKET_DB"] = str(settings.databases["market"])
    os.environ["INVEST_REPORTS_DB"] = str(settings.databases["reports"])
    os.environ["INVEST_STATE_DIR"] = str(settings.path(settings.config["paths"]["state_dir"]))


def main() -> None:
    # 分派统一命令，并只在需要时加载既有业务模块。
    args = _parser().parse_args()
    settings = load_settings()
    _set_runtime(settings)
    if args.group == "db":
        action = {"check": check_sources, "migrate": migrate, "verify": verify}[args.action]
        print(json.dumps(action(settings), ensure_ascii=False, indent=2))
        return
    if args.group == "doctor":
        print(json.dumps(_doctor(), ensure_ascii=False, indent=2))
        return
    if args.group == "market":
        module = {"pool": "security_pool.main", "stock": "stock_data.main",
                  "etf": "security_pool.etf_data"}[args.area]
        _delegate(module, _append_default(args.arguments, "--db", settings.databases["market"]))
        return
    if args.group == "research":
        if args.area == "factor":
            forwarded = _append_default(args.arguments, "--db", settings.databases["research"])
            forwarded = _append_default(forwarded, "--market-db", settings.databases["market"])
            _delegate("factor_strategy.main", forwarded)
            return
        if args.area == "mvp":
            _delegate("factor_strategy.mvp", _append_default(
                args.arguments, "--db", settings.databases["market"]
            ))
            return
        if args.area == "three-momentum":
            _delegate("factor_strategy.etf_rotation", _append_default(
                args.arguments, "--db", settings.databases["market"]
            ))
            return
        module = {"etf": "etf_strategy", "full-market": "etf_strategy.full_market",
                  "ten-year": "etf_strategy.ten_year"}[args.area]
        forwarded = _append_default(args.arguments, "--db", settings.databases["market"])
        if args.area == "etf":
            forwarded = _append_default(forwarded, "--results-db", settings.databases["research"])
        if args.area == "full-market":
            forwarded = _append_default(forwarded, "--results-db", settings.databases["research"])
        _delegate(module, forwarded)
        return
    if args.group == "accounts":
        forwarded = _append_default(args.arguments, "--results-db", settings.databases["accounts"])
        _delegate("etf_strategy", forwarded)
        return
    if args.group == "report":
        if args.area == "web":
            _delegate("daily_report.web", args.arguments)
            return
        forwarded = _append_default(args.arguments, "--source-db", settings.databases["market"])
        forwarded = _append_default(forwarded, "--report-db", settings.databases["reports"])
        _delegate("daily_report.cli", forwarded)
        return
    if args.group == "jobs":
        _delegate("daily_report.scheduler", args.arguments)
