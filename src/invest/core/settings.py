"""应用配置加载与项目路径解析。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    # 保存已解析的应用配置和数据库位置。
    root: Path
    config: dict[str, Any]

    def path(self, value: str) -> Path:
        # 将项目配置路径解析为绝对路径。
        target = Path(value)
        return target if target.is_absolute() else self.root / target

    @property
    def databases(self) -> dict[str, Path]:
        # 返回四个独立数据库的绝对路径。
        return {name: self.path(value) for name, value in self.config["databases"].items()}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    # 递归合并默认配置与本机覆盖配置。
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(root: Path | None = None) -> Settings:
    # 按默认配置、本机配置顺序加载，并创建运行目录。
    project_root = (root or ROOT).resolve()
    with (project_root / "config.toml").open("rb") as handle:
        config = tomllib.load(handle)
    local = project_root / "config.local.toml"
    if local.exists():
        with local.open("rb") as handle:
            config = _merge(config, tomllib.load(handle))
    env_market = os.getenv("INVEST_MARKET_DB")
    if env_market:
        config["databases"]["market"] = env_market
    settings = Settings(project_root, config)
    for section, key in (("paths", "database_dir"), ("paths", "state_dir"),
                         ("paths", "evidence_dir"), ("paths", "report_dir"),
                         ("paths", "log_dir"), ("paths", "backup_dir")):
        settings.path(config[section][key]).mkdir(parents=True, exist_ok=True)
    return settings
