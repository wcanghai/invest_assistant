"""Windows 定时检查：双批次重试与本地历史补偿。"""

import json
import os
import subprocess
import sys
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from invest.core.settings import load_settings
from invest.core.files import replace_with_retry

from invest.reporting.common import CONFIG
from invest.reporting.common import EXTERNAL
from invest.reporting.common import ROOT
from invest.reporting.common import SHANGHAI
from invest.reporting.common import SOURCE_DB
from invest.reporting.common import load_config
from invest.reporting.service import generate
from invest.providers.web import collect
from invest.providers.web import collection_lock

SETTINGS = load_settings()
STATE = SETTINGS.path(SETTINGS.config['paths']['state_dir']) / 'report_schedule.json'


def due_slots(now, state):
    # 返回今天已经到期但尚未成功的批次。
    day = now.date().isoformat()
    return [f'{day} {hour}' for hour in SETTINGS.config['scheduler']['report_slots']
            if now.strftime('%H:%M') >= hour and not state.get(f'{day} {hour}')]


def export_report(day, source_db=SOURCE_DB):
    # 生成指定日期快照并导出可独立查看的日报。
    report = generate(day, source_db=source_db)
    folder = SETTINGS.path(SETTINGS.config['paths']['report_dir']) / 'daily' / day
    folder.mkdir(parents=True, exist_ok=True)
    name = f"market_report_{day}_v{report['version']}.md"
    (folder / name).write_text(report['markdown'], encoding='utf-8')
    return report


def save_state(state):
    # 原子保存状态，进程中断不会留下半个 JSON 文件。
    temp = STATE.with_suffix('.tmp')
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    replace_with_retry(temp, STATE)


def sync_local(day):
    # 直接启动 Python 行情任务，隔离通达信运行时，沿用共用更新锁和完成核验。
    if datetime.fromisoformat(day).weekday() >= 5:
        return True
    python = SETTINGS.config.get('tdx', {}).get('python', sys.executable)
    command = [python, '-X', 'utf8', '-m', 'invest', 'jobs', 'market',
               '--date', day, '--db', str(SOURCE_DB), '--no-report']
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(ROOT / 'src')
    result = subprocess.run(command, cwd=ROOT, timeout=1800, check=False,
                            env=environment,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
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
        config = load_config(CONFIG)
        expected = sum(len(config[category]) for category in EXTERNAL)
        if expected > 0 and result['total'] == expected and result['failed'] == 0:
            for slot in slots:
                state[slot] = True
        print(f'COLLECT {result} report={report["status"]}', flush=True)
    except Exception as exc:
        print(f'COLLECT_ERROR {exc}', flush=True)
        export_report(now.date().isoformat())
    finally:
        save_state(state)


if __name__ == '__main__':
    with collection_lock(STATE.parent / 'report_scheduler.db'):
        run()
