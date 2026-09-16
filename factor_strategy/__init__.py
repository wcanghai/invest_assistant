"""简化版 A 股多因子选股策略。"""

from .config import DEFAULT_CONFIG, load_config
from .scoring import calculate_scores

__all__ = ["DEFAULT_CONFIG", "calculate_scores", "load_config"]
