"""验证研究档案保存完整参数、输入摘要和失败记录，不覆盖以前的运行。"""

import json
import sqlite3

import pytest

from invest.core.runs import option, research_run


def test_research_archives_inputs_and_preserves_previous_outputs(tmp_path):
    # 显式临时路径运行两次，输出独立目录且输入库没有建表或写入。
    market = tmp_path / "market.db"
    with sqlite3.connect(market) as connection:
        connection.execute("CREATE TABLE etf_daily (trade_date TEXT)")
        connection.execute("INSERT INTO etf_daily VALUES ('2026-09-16')")
    before = market.read_bytes()
    arguments = ["--db", str(market), "--output", str(tmp_path / "reports")]
    folders = []
    for _ in range(2):
        with research_run("invest.research.etf", arguments) as forwarded:
            from pathlib import Path

            folder = Path(option(forwarded, "--output"))
            (folder / "result.txt").write_text("result", encoding="utf-8")
            folders.append(folder)
    assert folders[0] != folders[1]
    assert market.read_bytes() == before
    record = json.loads((folders[0] / "run_manifest.json").read_text("utf-8"))
    assert record["status"] == "complete"
    assert record["strategy_config"]["fee_bps"] == 3.0
    assert record["inputs"]["etf_daily"] == {"rows": 1, "cutoff": "2026-09-16"}
    assert record["files"] == [{"path": "result.txt", "bytes": 6}]
    assert record["code"]["source_sha256"]


def test_failure_has_audit_record(tmp_path):
    # 业务中途失败也保留运行信息，不能留下伪成功状态。
    with pytest.raises(ValueError, match="sample failure"):
        with research_run("invest.research.etf", ["--output", str(tmp_path)]):
            raise ValueError("sample failure")
    record = json.loads(next(tmp_path.rglob("run_manifest.json")).read_text("utf-8"))
    assert record["status"] == "failed" and record["error"] == "sample failure"
