"""日报配置和通用数值处理。"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = Path(os.getenv("INVEST_MARKET_DB", ROOT / "data/security_pool.db"))
REPORT_DB = Path(os.getenv("INVEST_REPORTS_DB", ROOT / "data/daily_report.db"))
CONFIG = ROOT / "configs/daily_report_universe.json"
SHANGHAI = timezone(timedelta(hours=8))
CATEGORIES = {
    "a_share_stocks": "自选股票", "industry_etfs": "行业 ETF",
    "a_share_indices": "主要指数", "commodity_futures": "商品期货",
    "us_stocks": "美股", "crypto_pairs": "虚拟货币",
}
EXTERNAL = ("a_share_indices", "commodity_futures", "us_stocks", "crypto_pairs")


def now_iso():
    # 返回带上海时区的采集时间。
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def load_config(path=CONFIG):
    # 保留原始名单顺序并验证配置结构。
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for category in CATEGORIES:
        if not isinstance(config.get(category), dict):
            raise ValueError(f"配置缺少证券字典：{category}")
    return config


def number(value):
    # 把网页数字转换为有限浮点数，空值绝不填零。
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None
