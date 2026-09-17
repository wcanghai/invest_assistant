"""可复核的源码迁移：只改导入、资源定位和模块入口，不改变计算函数。"""

import argparse
import ast
import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "security_pool": "invest.market.security_pool",
    "stock_data": "invest.market.stock",
    "factor_strategy": "invest.research.stocks",
    "etf_strategy": "invest.research.etf",
    "daily_report": "invest.reporting",
}
OVERRIDES = {
    "tdx_client": "invest.providers.tdx_client",
    "security_pool.fetcher": "invest.providers.security_pool",
    "stock_data.fetcher": "invest.providers.stock",
    "daily_report.sources": "invest.providers.web",
    "security_pool.db": "invest.storage.security_pool",
    "stock_data.db": "invest.storage.stock",
    "factor_strategy.db": "invest.storage.factor",
    "etf_strategy.storage": "invest.storage.etf",
    "daily_report.storage": "invest.storage.reports",
    "factor_strategy.etf_rotation": "invest.research.etf.three_momentum",
    "etf_strategy.collect": "invest.market.etf_collect",
    "etf_strategy.manual": "invest.accounts.manual",
    "daily_report.scheduler": "invest.jobs.report",
    "scripts.check_daily_sync_status": "invest.jobs.status",
}


def mapped(name):
    # 优先映射职责拆分后的模块，再保留所属包内的相对层次。
    if name in OVERRIDES:
        return OVERRIDES[name]
    for old, new in PACKAGES.items():
        if name == old or name.startswith(old + "."):
            return new + name[len(old):]
    return name


def rewrite(source, module, package=False):
    # 用语法树定位导入，逐段替换，保留算法正文和注释的原始文本。
    lines = source.splitlines(keepends=True)
    changes = []
    parent = module if package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        output = []
        if isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = importlib.util.resolve_name("." * node.level + name, parent)
            for item in node.names:
                child = name + "." + item.name
                if child in OVERRIDES:
                    destination, _, symbol = mapped(child).rpartition(".")
                    alias = item.asname or item.name
                    output.append(f"from {destination} import {symbol} as {alias}")
                else:
                    alias = " as " + item.asname if item.asname else ""
                    output.append(f"from {mapped(name)} import {item.name}{alias}")
        else:
            for item in node.names:
                if mapped(item.name) != item.name and not item.asname and "." in item.name:
                    raise ValueError(f"需要手工迁移模块绑定：{module}: {item.name}")
                alias = " as " + item.asname if item.asname else ""
                output.append(f"import {mapped(item.name)}{alias}")
        indent = " " * node.col_offset
        changes.append((node.lineno - 1, node.end_lineno,
                        "\n".join(indent + value for value in output) + "\n"))
    for start, end, value in sorted(changes, reverse=True):
        lines[start:end] = [value]
    result = "".join(lines)
    if "Path(__file__).resolve().parents[1]" in result:
        result = result.replace("Path(__file__).resolve().parents[1]", "PROJECT_HOME")
        tree = ast.parse(result)
        insertion = 1 if ast.get_docstring(tree) else 0
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                insertion = node.end_lineno
        parts = result.splitlines(keepends=True)
        parts.insert(insertion, "\nfrom invest.core.settings import ROOT as PROJECT_HOME\n")
        result = "".join(parts)
    schemas = {"security_pool.db": "security_pool.sql", "stock_data.db": "stock.sql",
               "factor_strategy.db": "factor.sql", "etf_strategy.storage": "etf.sql"}
    if module in schemas:
        result = result.replace('with_name("schema.sql")', f'with_name("{schemas[module]}")')
    string_modules = {**PACKAGES, **OVERRIDES}
    for node in ast.walk(ast.parse(result)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if mapped(value) != value and (ROOT / (value.replace(".", "/") + ".py")).is_file():
                string_modules[value] = mapped(value)
    for old, new in string_modules.items():
        if old.endswith(".db"):
            continue
        for quote in ("'", '"'):
            result = result.replace(quote + old + quote, quote + new + quote)
    for old, new in (("security_pool", "security_pool"), ("stock_data", "stock")):
        result = result.replace(f"{old}/schema.sql", f"src/invest/storage/{new}.sql")
    return result


def stage():
    # 先创建新包和资源，旧任务仍可继续运行；输出逐文件迁移清单。
    manifest = {}
    paths = [ROOT / "tdx_client.py", ROOT / "scripts/check_daily_sync_status.py"]
    for package in PACKAGES:
        paths.extend((ROOT / package).glob("*.py"))
    for path in paths:
        relative = path.relative_to(ROOT).with_suffix("")
        name = ".".join(relative.parts)
        is_package = path.name == "__init__.py"
        if is_package:
            name = name.removesuffix(".__init__")
        destination = ROOT / "src" / Path(*mapped(name).split("."))
        destination = destination / "__init__.py" if is_package else destination.with_suffix(".py")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rewrite(path.read_text("utf-8-sig"), name, is_package), "utf-8")
        manifest[str(path.relative_to(ROOT))] = str(destination.relative_to(ROOT))
    for old, new in (("security_pool", "security_pool"), ("stock_data", "stock"),
                     ("factor_strategy", "factor"), ("etf_strategy", "etf")):
        shutil.copy2(ROOT / old / "schema.sql", ROOT / f"src/invest/storage/{new}.sql")
    shutil.copytree(ROOT / "daily_report/static", ROOT / "src/invest/reporting/static",
                    dirs_exist_ok=True)
    (ROOT / ".tmp/module_relocation.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")


def wire():
    # 把验证代码和统一入口切换到已暂存的新模块；仅执行一次。
    paths = list((ROOT / "tests").glob("*.py")) + [ROOT / "src/invest/cli.py"]
    for path in paths:
        name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        path.write_text(rewrite(path.read_text("utf-8-sig"), name), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "wire"))
    arguments = parser.parse_args()
    {"stage": stage, "wire": wire}[arguments.action]()
