"""简化版 A 股多因子选股策略。"""

from invest.research.stocks.config import DEFAULT_CONFIG
from invest.research.stocks.config import load_config
from invest.research.stocks.scoring import calculate_scores

__all__ = ["DEFAULT_CONFIG", "calculate_scores", "load_config"]
