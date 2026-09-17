"""研究运行档案：完整参数、代码指纹、输入摘要及输出文件清单。"""

import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from invest.core.settings import load_settings

SHANGHAI = timezone(timedelta(hours=8))


def option(arguments, name, default=None):
    # 同时解析空格和等号形式，保留命令行原值以供审计。
    for index, value in enumerate(arguments):
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
        if value == name and index + 1 < len(arguments):
            return arguments[index + 1]
    return default


def replace_option(arguments, name, value):
    # 用运行专属目录替换输出目录，显式相对目录按调用目录解析。
    result = list(arguments)
    for index, item in enumerate(result):
        if item.startswith(name + "="):
            result[index] = f"{name}={value}"
            return result
        if item == name:
            result[index + 1] = str(value)
            return result
    return [*result, name, str(value)]


def code_version(root):
    # 同时保存提交号和实际源码哈希，使未提交修改仍然可准确识别。
    digest = hashlib.sha256()
    for path in sorted((root / "src/invest").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False)
    return {"git_commit": result.stdout.strip() if result.returncode == 0 else None,
            "source_sha256": digest.hexdigest()}


def input_summary(path):
    # 只读描述输入库关键表的行数和截止日期，不初始化或修改任何业务表。
    path = Path(path).resolve()
    result = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    result.update(bytes=path.stat().st_size, modified_ns=path.stat().st_mtime_ns)
    tables = {"stock_current": "data_date", "stock_daily_bar": "trade_date",
              "stock_financial_report": "report_date", "etf_daily": "trade_date"}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        existing = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in tables.items():
            if table not in existing:
                continue
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                continue
            count, cutoff = connection.execute(
                f'SELECT COUNT(*), MAX("{column}") FROM "{table}"').fetchone()
            result[table] = {"rows": count, "cutoff": cutoff}
    return result


def effective_config(module, arguments):
    # 按业务原加载器展开策略默认值与显式覆盖值，避免遗漏隐含参数。
    path = option(arguments, "--config")
    if module.endswith("three_momentum"):
        return json.loads(Path(path).read_text("utf-8-sig"))
    if ".stocks." in module:
        from invest.research.stocks.config import load_config
    else:
        from invest.research.etf.config import load_config
    return load_config(path)


@contextmanager
def research_run(module, arguments):
    # 每次真实研究生成独立档案，失败也保存原因，不覆盖已有结果。
    settings = load_settings()
    output_option = "--output-dir" if module.endswith(".mvp") else "--output"
    root = option(arguments, output_option,
                  str(settings.path(settings.config["paths"]["report_dir"]) / "factor"))
    now = datetime.now(SHANGHAI)
    run_id = uuid.uuid4().hex
    output = Path(root).resolve() / now.date().isoformat() / run_id
    output.mkdir(parents=True, exist_ok=False)
    market = option(arguments, "--market-db", option(arguments, "--db"))
    record = {"run_id": run_id, "module": module, "arguments": list(arguments),
              "started_at": now.isoformat(), "status": "running", "output": str(output),
              "application_config": settings.config, "code": code_version(settings.root)}
    forwarded = replace_option(arguments, output_option, output)
    manifest = output / "run_manifest.json"
    try:
        record["strategy_config"] = effective_config(module, arguments)
        record["inputs"] = input_summary(market) if market else None
        record["requested_cutoff"] = option(arguments, "--end", option(arguments, "--date"))
        record["resolved_arguments"] = forwarded
        yield forwarded
        record["status"] = "complete"
    except BaseException as error:
        record.update(status="failed", error=str(error))
        raise
    finally:
        record["finished_at"] = datetime.now(SHANGHAI).isoformat()
        record["files"] = [{"path": path.relative_to(output).as_posix(),
                            "bytes": path.stat().st_size}
                           for path in sorted(output.rglob("*")) if path.is_file()]
        manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
        print(f"运行档案：{manifest}", file=sys.stderr, flush=True)
