"""行情任务的失败恢复、并发排除和完成状态回归测试。"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from invest.core.locking import JobBusy, file_lock
from invest.jobs import market
from invest.core.files import replace_with_retry


def _status(complete=False, current="2026-09-16", missing=None):
    # 提供只有业务状态的独立副本，测试不会接触正式数据库。
    return {"complete": complete,
            "stock": {"complete": complete, "current_date": current,
                      "missing_codes": missing or ["600001.SH"]},
            "etf": {"complete": complete}}


def test_duplicate_lock_cannot_mark_success(tmp_path):
    # 重复触发不允许进入更新，也不能写出新的成功状态。
    database = tmp_path / "market.db"
    with file_lock(database.with_suffix(".update.lock")):
        with pytest.raises(JobBusy):
            market.run_market("2026-09-16", database, tmp_path / "state")
    assert not (tmp_path / "state").exists()


def test_non_trading_day_and_invalid_calendar(monkeypatch, tmp_path):
    # 周末无需连接接口；接口返回 None 不得冒充休市。
    init = Mock(side_effect=RuntimeError("TDX unavailable"))
    monkeypatch.setattr(market, "init_tdx", init)
    database = tmp_path / "market.db"
    result = market.run_market("2026-09-13", database, tmp_path / "state")
    assert result["status"] == "non_trading_day"
    init.assert_not_called()
    monkeypatch.setattr(market, "get_status", Mock(return_value=_status()))
    with pytest.raises(RuntimeError, match="TDX unavailable"):
        market.run_market("2026-09-16", database, tmp_path / "state")
    saved = json.loads((tmp_path / "state/market/2026-09-16.json").read_text("utf-8"))
    assert saved["status"] == "failed" and saved["stage"] == "calendar"
    client = Mock()
    client.get_trading_dates.return_value = None
    with pytest.raises(RuntimeError, match="有效交易日历"):
        market.run_market("2026-09-16", database, tmp_path / "state", client=client)
    client.get_trading_dates.return_value = []
    result = market.run_market("2026-09-16", database, tmp_path / "state", client=client)
    assert result["status"] == "non_trading_day"


def test_partial_retry_and_verified_completion(monkeypatch, tmp_path):
    # 只重试缺失股票，最终核验不完整则失败，重试核验通过后才完成。
    database = tmp_path / "market.db"
    client = Mock()
    client.get_trading_dates.return_value = ["20260916"]
    status = Mock(return_value=_status())
    monkeypatch.setattr(market, "get_status", status)
    pool, stocks, etfs, check = Mock(), Mock(), Mock(), Mock()
    for name, value in (("update_pool", pool), ("update_stocks", stocks),
                        ("load_etf_data", etfs), ("check_data", check)):
        monkeypatch.setattr(market, name, value)
    with pytest.raises(RuntimeError, match="仍有数据未完成"):
        market.run_market("2026-09-16", database, tmp_path / "state", client=client)
    pool.assert_not_called()
    assert stocks.call_args.args[2] == ["600001.SH"]
    status.side_effect = [_status(), _status(complete=True)]
    result = market.run_market("2026-09-16", database, tmp_path / "state", client=client)
    assert result["status"] == "complete"
    status.side_effect = None
    status.return_value = _status(complete=True)
    stocks.reset_mock()
    result = market.run_market("2026-09-16", database, tmp_path / "state", client=client)
    assert result["status"] == "complete"
    stocks.assert_not_called()


def test_history_refreshes_pool_and_batches(monkeypatch, tmp_path):
    # 落后证券池先更新，再按原批量范围同步，不把空证券池当成成功。
    client = Mock()
    client.get_trading_dates.return_value = ["20260916"]
    monkeypatch.setattr(market, "get_status", Mock(side_effect=[
        _status(current="2026-09-15"), _status(complete=True)]))
    pool, stocks = Mock(), Mock()
    monkeypatch.setattr(market, "update_pool", pool)
    monkeypatch.setattr(market, "update_stocks", stocks)
    monkeypatch.setattr(market, "load_etf_data", Mock())
    monkeypatch.setattr(market, "check_data", Mock())
    monkeypatch.setattr(market, "load_scope_codes", Mock(return_value=["a", "b", "c"]))
    market.run_market("2026-09-16", tmp_path / "market.db", tmp_path / "state", 2, client)
    pool.assert_called_once()
    assert [call.args[2] for call in stocks.call_args_list] == [["a", "b"], ["c"]]


def test_windows_temporary_file_contention(monkeypatch, tmp_path):
    # 短暂共享冲突应重试，长期权限错误仍失败且原状态不受破坏。
    source, target = tmp_path / "new.tmp", tmp_path / "state.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    original = Path.replace
    failures = [True, True, False]

    def flaky_replace(path, destination):
        # 模拟 Windows 防病毒扫描期间拒绝替换随后恢复正常的行为。
        if failures.pop(0):
            raise PermissionError("temporary contention")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    replace_with_retry(source, target)
    assert target.read_text("utf-8") == "new"
    monkeypatch.setattr(Path, "replace", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError):
        replace_with_retry(source, target, attempts=2)
    assert target.read_text("utf-8") == "new"
