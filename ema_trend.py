"""Тренд-фильтр по EMA: вердикт для сигналов памп/дамп."""
import numpy as np

EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200
EMA_PULLBACK = 20
EMA_DISTANCE_THRESHOLD_PCT = 0.5

TREND_INTERVAL = "1h"
TREND_LIMIT = 210
PULLBACK_INTERVAL = "15m"
PULLBACK_LIMIT = 30


def calc_ema(closes: np.ndarray, period: int) -> float:
    """EMA: seed — SMA первых `period` значений, затем рекурсивно."""
    if len(closes) < period:
        raise ValueError(f"нужно минимум {period} свечей, получено {len(closes)}")
    alpha = 2 / (period + 1)
    ema = closes[:period].mean()
    for price in closes[period:]:
        ema = alpha * price + (1 - alpha) * ema
    return float(ema)
