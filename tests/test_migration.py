"""数据库拆分迁移的结构和内容核对测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from invest.core.settings import load_settings
from invest.storage import migration


def _database(path: Path, statements: list[str]) -> None:
    # 创建用于迁移测试的最小 SQLite 源库。
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)


def _settings(tmp_path: Path):
    # 创建与正式配置结构一致的临时项目配置。
    (tmp_path / "data").mkdir()
    (tmp_path / "config.toml").write_text(
        "[paths]\ndatabase_dir='data/databases'\nstate_dir='data/state'\n"
        "evidence_dir='data/evidence'\nreport_dir='reports'\nlog_dir='logs'\n"
        "backup_dir='backups'\n[databases]\nmarket='data/databases/market.db'\n"
        "research='data/databases/research.db'\naccounts='data/databases/accounts.db'\n"
        "reports='data/databases/reports.db'\n",
        encoding="utf-8",
    )
    return load_settings(tmp_path)


def test_migration_rejects_unknown_source_tables(tmp_path: Path) -> None:
    # 遇到未映射业务表时迁移必须停止，避免静默丢失数据。
    settings = _settings(tmp_path)
    _database(tmp_path / "data/security_pool.db", ["CREATE TABLE unexpected (id INTEGER)"])
    _database(tmp_path / "data/etf_strategy.db", ["CREATE TABLE research_run (id TEXT)"])
    _database(tmp_path / "data/daily_report.db", ["CREATE TABLE reports (id INTEGER)"])
    try:
        migration.check_sources(settings)
    except RuntimeError as error:
        assert "unknown" in str(error)
    else:
        raise AssertionError("unknown table must reject migration")
