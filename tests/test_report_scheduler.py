"""检查调度成功记账、失败重试及昨日补偿。"""

from datetime import datetime
from unittest.mock import Mock

from daily_report import scheduler as s
from daily_report.common import SHANGHAI


def test_slots():
    # 上午只执行早间批次，下午补齐到期批次，成功后不再运行。
    morning = datetime(2026, 9, 12, 8, tzinfo=SHANGHAI)
    assert s.due_slots(morning, {}) == ['2026-09-12 08:00']
    assert s.due_slots(morning, {'2026-09-12 08:00': True}) == []
    assert len(s.due_slots(morning.replace(hour=16), {})) == 2


def test_retry_and_success(monkeypatch, tmp_path):
    # 部分采集失败不记账，完整采集且导出后才记账。
    monkeypatch.setattr(s, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(s, 'export_report', Mock(return_value={'status': 'partial'}))
    monkeypatch.setattr(s, 'collect', Mock(return_value={'total': 24, 'failed': 1}))
    now = datetime(2026, 9, 12, 16, tzinfo=SHANGHAI)
    state = {}
    slots = s.due_slots(now, state)
    s.collect_due(now, state, slots)
    assert not state
    s.collect.return_value = {'total': 24, 'failed': 0}
    s.collect_due(now, state, slots)
    assert not s.due_slots(now, state)


def test_backfill_retry(monkeypatch, tmp_path):
    # 昨日同步失败仍生成报告，保留待办以便下次继续补偿。
    monkeypatch.setattr(s, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(s, 'due_slots', lambda now, state: [])
    monkeypatch.setattr(s, 'sync_local', Mock(return_value=False))
    monkeypatch.setattr(s, 'export_report', Mock(return_value={'status': 'partial'}))
    s.run()
    state = s.json.loads(s.STATE.read_text('utf-8'))
    assert list(state['backfill'].values()) == [False]
    s.sync_local.return_value = True
    s.run()
    state = s.json.loads(s.STATE.read_text('utf-8'))
    assert list(state['backfill'].values()) == [True]
