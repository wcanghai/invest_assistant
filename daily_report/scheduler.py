"""Windows 定时检查：双批次重试与本地历史补偿。"""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from .common import ROOT, SHANGHAI
from .service import generate
from .sources import collect, collection_lock

STATE = Path(os.getenv('INVEST_STATE_DIR', ROOT / 'data')) / 'report_schedule.json'


def due_slots(now, state):
    # 返回今天已经到期但尚未成功的批次。
    day = now.date().isoformat()
    return [f'{day} {hour}' for hour in ('08:00', '15:30')
            if now.strftime('%H:%M') >= hour and not state.get(f'{day} {hour}')]


def export_report(day):
    # 生成指定日期快照并导出可独立查看的日报。
    report = generate(day)
    folder = ROOT / 'reports'
    folder.mkdir(exist_ok=True)
    name = f"market_report_{day}_v{report['version']}.md"
    (folder / name).write_text(report['markdown'], encoding='utf-8')
    return report


def save_state(state):
    # 原子保存状态，进程中断不会留下半个 JSON 文件。
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(STATE)


def sync_local(day):
    # 调用已有按日同步器，子进程继承静默窗口并设置超时。
    if datetime.fromisoformat(day).weekday() >= 5:
        return True
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle',
               'Hidden', '-ExecutionPolicy', 'Bypass', '-File',
               str(ROOT / 'scripts/run_daily_market_sync.ps1'), '-DataDate', day]
    result = subprocess.run(command, cwd=ROOT, timeout=1800, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode == 0


def run():
    # 每次检查独立退出，失败状态留待 Windows 十分钟后重试。
    now = datetime.now(SHANGHAI)
    state = json.loads(STATE.read_text('utf-8')) if STATE.exists() else {}
    slots = due_slots(now, state)
    if slots:
        collect_due(now, state, slots)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    state.setdefault('backfill', {}).setdefault(yesterday, False)
    for day, done in list(state['backfill'].items()):
        if done:
            continue
        try:
            local_ok = sync_local(day)
            report = export_report(day)
            state['backfill'][day] = local_ok
            print(f'BACKFILL {day} local={local_ok} report={report["status"]}', flush=True)
        except Exception as exc:
            print(f'BACKFILL_ERROR {day}: {exc}', flush=True)
        save_state(state)


def collect_due(now, state, slots):
    # 到期批次采集成功后记账，部分失败留待下次重试。
    try:
        result = collect()
        report = export_report(now.date().isoformat())
        if result['total'] > 0 and result['failed'] == 0:
            for slot in slots:
                state[slot] = True
        print(f'COLLECT {result} report={report["status"]}', flush=True)
    except Exception as exc:
        print(f'COLLECT_ERROR {exc}', flush=True)
        export_report(now.date().isoformat())
    finally:
        save_state(state)


if __name__ == '__main__':
    state_dir = Path(os.getenv('INVEST_STATE_DIR', ROOT / 'data'))
    with collection_lock(state_dir / 'report_scheduler.db'):
        run()
