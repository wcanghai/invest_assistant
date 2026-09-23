"""管理日报股票与 ETF 自选名单，并兼容旧的增量跟踪文件。"""

import json
import threading
from pathlib import Path

from invest.reporting.common import CONFIG
from invest.reporting.common import TRACKING
from invest.reporting.common import ROOT

WATCHLIST = ROOT / "data/daily_watchlist.json"
CATEGORIES = ("a_share_stocks", "industry_etfs")
_LOCK = threading.RLock()


def _read_json(path: Path) -> dict:
    # 读取 JSON 对象，不存在时返回空对象。
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"自选文件不是 JSON 对象: {path}")
    return value


def _write(path: Path, value: dict) -> None:
    # 使用同目录临时文件原子替换自选文件。
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load(path: Path = WATCHLIST, config_path: Path = CONFIG,
         tracking_path: Path = TRACKING) -> dict[str, dict[str, str]]:
    # 首次读取时合并原始配置和旧增量名单，之后使用统一文件。
    path = Path(path)
    with _LOCK:
        if path.is_file():
            current = _read_json(path)
        else:
            config = _read_json(Path(config_path))
            tracking = _read_json(Path(tracking_path))
            current = {}
            for category in CATEGORIES:
                current[category] = dict(config.get(category, {}))
                current[category].update(tracking.get(category, {}))
            _write(path, current)
        return {category: dict(current.get(category, {})) for category in CATEGORIES}


def save(items: dict[str, dict[str, str]], path: Path = WATCHLIST) -> None:
    # 校验并持久化完整自选集合。
    normalized = {}
    for category in CATEGORIES:
        values = items.get(category, {})
        if not isinstance(values, dict):
            raise ValueError("自选分类必须是代码到名称的映射")
        normalized[category] = {str(code): str(name) for code, name in values.items()}
    with _LOCK:
        _write(Path(path), normalized)


def add(category: str, code: str, name: str, path: Path = WATCHLIST) -> dict:
    # 幂等加入一个自选标的。
    if category not in CATEGORIES:
        raise ValueError("不支持的自选分类")
    with _LOCK:
        items = load(path)
        items[category][code] = name
        save(items, path)
        return items


def remove(category: str, code: str, path: Path = WATCHLIST) -> dict:
    # 幂等删除一个自选标的。
    if category not in CATEGORIES:
        raise ValueError("不支持的自选分类")
    with _LOCK:
        items = load(path)
        items[category].pop(code, None)
        save(items, path)
        return items
