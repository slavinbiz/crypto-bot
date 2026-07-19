"""Контр-сигнал по недельным EMA: вход против направления памп/дамп сигнала.

Идея: после резкого движения ищем откат к ближайшей недельной EMA (7/14/28)
с противоположной стороны и предлагаем вход туда — лимитка на уровне EMA,
стоп за следующей более дальней EMA (с буфером), фиксированный тейк.
"""
import numpy as np

from ema_trend import calc_ema

WEEKLY_INTERVAL = "1w"
WEEKLY_LIMIT = 40
EMA_PERIODS = [7, 14, 28]
STOP_BUFFER_PCT = 2.0
TAKE_PROFIT_PCT = 3.0


def calc_weekly_emas(closes: np.ndarray) -> dict[int, float]:
    """EMA по каждому периоду, для которого хватает недельных свечей. Недостающие пропускаются."""
    return {period: calc_ema(closes, period) for period in EMA_PERIODS if len(closes) >= period}


def build_pullback_signal(direction: str, price: float, weekly_candles: list[dict]) -> dict | None:
    """direction — направление ИСХОДНОГО памп/дамп сигнала ("long" на пампе, "short" на дампе).
    Контр-сигнал открывается в противоположную сторону. None — если недельной структуры не хватает."""
    closes = np.array([c["close"] for c in weekly_candles])
    emas = calc_weekly_emas(closes)

    counter_direction = "short" if direction == "long" else "long"

    if counter_direction == "long":
        side = {p: v for p, v in emas.items() if v < price}
    else:
        side = {p: v for p, v in emas.items() if v > price}

    if len(side) < 2:
        return None

    pick_entry = max if counter_direction == "long" else min
    entry_period = pick_entry(side, key=lambda p: side[p])
    entry = side[entry_period]

    further = {p: v for p, v in side.items() if p != entry_period}
    stop_period = pick_entry(further, key=lambda p: further[p])
    stop_base = further[stop_period]

    if counter_direction == "long":
        stop = stop_base * (1 - STOP_BUFFER_PCT / 100)
        take = entry * (1 + TAKE_PROFIT_PCT / 100)
    else:
        stop = stop_base * (1 + STOP_BUFFER_PCT / 100)
        take = entry * (1 - TAKE_PROFIT_PCT / 100)

    return {
        "direction": counter_direction,
        "entry_period": entry_period,
        "entry": entry,
        "stop": stop,
        "take": take,
        "stop_period": stop_period,
        "emas": emas,
    }
