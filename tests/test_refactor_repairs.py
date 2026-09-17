"""回归覆盖分库连接、工作目录独立性以及显式参数优先级。"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

from invest.storage import factor as db
from invest.research.stocks.config import load_config
from invest.research.stocks.main import score_date
from invest.research.stocks.backtest import run_backtest
from invest.cli import _append_default
from invest.cli import _absolute_paths
from invest.core.settings import load_settings
from tests.test_factor_strategy import _seed_database


def test_split_connection_preserves_scores_and_isolates_writes(tmp_path):
    # 对同一数据比较完整评分，并验证市场只读且研究结果可写。
    market, research = tmp_path / 'market.db', tmp_path / 'research.db'
    with closing(_seed_database()) as seed:
        expected = score_date(seed, '2026-08-31', load_config())
        with closing(sqlite3.connect(market)) as target:
            seed.backup(target)
    with closing(db.connect(research, market)) as connection:
        db.init_strategy_schema(connection)
        actual = score_date(connection, '2026-08-31', load_config())
        pd.testing.assert_frame_equal(expected, actual, rtol=1e-10, atol=1e-12)
        connection.execute('CREATE TABLE result_probe (value INTEGER)')
        connection.execute('INSERT INTO result_probe VALUES (1)')
        connection.commit()
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            connection.execute('DELETE FROM market_source.stock_master')
        assert connection.execute('SELECT COUNT(*) FROM stock_corporate_action').fetchone()
        assert connection.execute('SELECT COUNT(*) FROM stock_snapshot').fetchone()
    with closing(sqlite3.connect(market)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='result_probe'"
        ).fetchone() is None


def test_split_backtest_preserves_nav_and_fills(tmp_path):
    # 比较相同数据在分库前后的净值与成交，仅忽略随机标识。
    market = tmp_path / 'market.db'
    config = load_config()
    config['portfolio'].update(target_count=5, buy_rank_pct=0.80, hold_rank_pct=0.90,
                               max_stock_weight=0.30, max_industry_weight=0.60)
    with closing(_seed_database()) as seed:
        expected = run_backtest(seed, '2026-06-01', '2026-08-31', config)
        with closing(sqlite3.connect(market)) as target:
            seed.backup(target)
    with closing(db.connect(tmp_path / 'research.db', market)) as connection:
        actual = run_backtest(connection, '2026-06-01', '2026-08-31', config)
    for key in ('nav', 'trades'):
        left = expected[key].drop(columns=['backtest_id', 'order_id'], errors='ignore')
        right = actual[key].drop(columns=['backtest_id', 'order_id'], errors='ignore')
        pd.testing.assert_frame_equal(left, right, rtol=1e-10, atol=1e-12)


def test_account_import_keeps_research_records_out_of_ledger(tmp_path):
    # 通过真实统一入口验证账户账本和研究记录分离，重复导入不会新增账户。
    snapshot = tmp_path / 'account.json'
    snapshot.write_text(json.dumps({'account_id': 'repair_test', 'as_of': '2026-09-16',
                                    'cash': 1000, 'holdings': {}}), encoding='utf-8')
    account, research = tmp_path / 'accounts.db', tmp_path / 'research.db'
    args = [sys.executable, '-X', 'utf8', '-m', 'invest', 'accounts', 'import-account',
            '--results-db', str(account), '--research-db', str(research),
            '--input', str(snapshot), '--output', str(tmp_path / 'output')]
    for _ in range(2):
        result = subprocess.run(args, cwd=tmp_path, capture_output=True)
        assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(account)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM manual_account').fetchone() == (1,)
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='research_run'"
        ).fetchone()
    with closing(sqlite3.connect(research)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM research_run').fetchone()[0] > 0


def test_status_check_never_creates_empty_database(tmp_path):
    # 复现本次误生成空市场库的入口，确保读失败时不落空文件。
    from invest.jobs.status import get_status

    path = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        get_status(path, '2026-09-16')
    assert not path.exists()


def test_cli_outside_project_reads_packaged_modules(tmp_path):
    # 在任意目录运行真实业务入口，无需手工 PYTHONPATH。
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    for arguments in (['market', 'pool', 'init-db', '--help'],
                      ['research', 'factor', 'score', '--help'],
                      ['report', 'run', 'list', '--help']):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'invest', *arguments],
                                cwd=tmp_path, env=env, capture_output=True,
                                text=True, encoding='utf-8')
        assert result.returncode == 0, result.stderr


def test_explicit_equals_parameter_is_not_overridden():
    # 同时支持 --db value 与 --db=value 两种显式写法。
    assert _append_default(['--db=x'], '--db', Path('y')) == ['--db=x']
    assert _append_default(['--db', 'x'], '--db', Path('y')) == ['--db', 'x']


def test_explicit_relative_paths_use_invocation_directory(monkeypatch, tmp_path):
    # 等号与分隔形式均按调用目录展开，纯配置读取不创建目录。
    monkeypatch.chdir(tmp_path)
    assert _absolute_paths(['--db=sample.db', '--output', 'reports']) == [
        '--db=' + str(tmp_path / 'sample.db'), '--output', str(tmp_path / 'reports')]
    assert not list(tmp_path.iterdir())


def test_settings_does_not_create_directories(tmp_path):
    # 配置读取不应产生运行目录或受内部环境变量污染。
    (tmp_path / 'config.toml').write_text('[databases]\nmarket="market.db"\n', encoding='utf-8')
    assert load_settings(tmp_path).databases['market'] == tmp_path / 'market.db'
    assert list(tmp_path.iterdir()) == [tmp_path / 'config.toml']
