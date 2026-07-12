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


def get_trend_verdict(direction: str, price: float, trend_candles: list[dict], pullback_candles: list[dict]) -> dict:
    """Вердикт по 1H-тренду (EMA50/EMA200) и положению цены к EMA20 на 15m."""
    trend_closes = np.array([c["close"] for c in trend_candles])
    pullback_closes = np.array([c["close"] for c in pullback_candles])

    ema_fast = calc_ema(trend_closes, EMA_TREND_FAST)
    ema_slow = calc_ema(trend_closes, EMA_TREND_SLOW)
    trend_up = ema_fast > ema_slow

    ema_pullback = calc_ema(pullback_closes, EMA_PULLBACK)
    signed_distance_pct = (price - ema_pullback) / ema_pullback * 100
    distance_pct = abs(signed_distance_pct)

    trend_matches = (direction == "long" and trend_up) or (direction == "short" and not trend_up)

    if not trend_matches:
        return {
            "verdict": "against_trend",
            "label": "🔴 Против тренда",
            "distance_pct": distance_pct,
        }
    if distance_pct <= EMA_DISTANCE_THRESHOLD_PCT:
        return {
            "verdict": "trend_at_ema",
            "label": f"🟢 По тренду, у EMA20 ({signed_distance_pct:+.1f}%)",
            "distance_pct": distance_pct,
        }
    return {
        "verdict": "trend_far_ema",
        "label": f"🟡 По тренду, далеко от EMA20 ({signed_distance_pct:+.1f}%)",
        "distance_pct": distance_pct,
    }
