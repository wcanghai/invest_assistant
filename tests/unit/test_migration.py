"""数据库拆分迁移的结构和内容核对测试。"""

from __future__ import annotations

import sqlite3
import json
from contextlib import closing
from pathlib import Path

import pytest

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


def _seed_sources(settings):
    # 构造包含自增删除空洞、索引、触发器和无 rowid 表的完整源清单。
    groups = (("market", migration.ALL_SOURCE_TABLES),
              ("etf_results", migration.ETF_RESULT_TABLES),
              ("reports", migration.REPORT_TABLES))
    for name, tables in groups:
        with closing(sqlite3.connect(migration._source_paths(settings)[name])) as connection:
            with connection:
                for table in tables:
                    if table == "research_run":
                        connection.execute(
                            f'CREATE TABLE "{table}" '
                            '(id TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID'
                        )
                        connection.execute(f'INSERT INTO "{table}" VALUES (?,?)', ('a', '原始'))
                    else:
                        connection.execute(
                            f'CREATE TABLE "{table}" '
                            '(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)'
                        )
                        connection.execute(f'INSERT INTO "{table}" VALUES (1,?)', ('原始',))
                        connection.execute(f'INSERT INTO "{table}" VALUES (80,?)', ('删除',))
                        connection.execute(f'DELETE FROM "{table}" WHERE id=80')
                        connection.execute(f'CREATE INDEX "idx_{table}" ON "{table}" (value)')
                if name == "reports":
                    connection.execute(
                        "CREATE TRIGGER report_audit AFTER INSERT ON reports "
                        "BEGIN INSERT INTO runs(value) VALUES ('trigger'); END"
                    )


def test_complete_migration_and_verification(tmp_path):
    # 验证成功迁移、历史序列保留、结构一致及重复执行拒绝覆盖。
    settings = _settings(tmp_path)
    _seed_sources(settings)
    outcome = migration.migrate(settings)
    assert outcome['verified_tables'] == 37
    assert migration.verify(settings)['status'] == 'ok'
    with closing(sqlite3.connect(settings.databases['reports'])) as connection:
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='reports'"
        ).fetchone() == (80,)
        with connection:
            connection.execute("INSERT INTO reports(value) VALUES ('new')")
        assert connection.execute('SELECT MAX(id) FROM reports').fetchone() == (81,)
        assert connection.execute("SELECT value FROM runs WHERE id=81").fetchone() == ('trigger',)
    with pytest.raises(FileExistsError):
        migration.migrate(settings)
    with pytest.raises(RuntimeError, match='核对失败'):
        migration.verify(settings)


def test_migration_uses_frozen_backup_and_does_not_publish_on_failure(tmp_path, monkeypatch):
    # 验证迁移从快照读取，复制失败不会发布任何正式库，且句柄已关闭。
    settings = _settings(tmp_path)
    _seed_sources(settings)
    original = migration._copy_tables

    def fail_after_copy(source, target, tables, append=False):
        # 确认复制来源位于备份目录，并模拟复制阶段失败。
        assert 'pre_migration_' in str(source)
        original(source, target, tables, append)
        raise RuntimeError('injected failure')

    monkeypatch.setattr(migration, '_copy_tables', fail_after_copy)
    with pytest.raises(RuntimeError, match='injected failure'):
        migration.migrate(settings)
    assert not any(path.exists() for path in settings.databases.values())
    staged = next((tmp_path / 'backups').glob('*/staging/market.db'))
    staged.rename(staged.with_suffix('.retained'))


def test_existing_targets_fail_before_source_scan(tmp_path):
    # 提前拒绝已有目标，不浪费时间全库扫描，也不覆盖空文件。
    settings = _settings(tmp_path)
    target = settings.databases['market']
    target.parent.mkdir(parents=True)
    target.touch()
    with pytest.raises(FileExistsError):
        migration.migrate(settings)
