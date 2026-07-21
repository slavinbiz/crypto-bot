"""Контр-сигнал по недельным EMA: вход против направления памп/дамп сигнала.

Идея: после резкого движения ищем откат к ближайшей недельной EMA (7/14/28)
с противоположной стороны и предлагаем вход туда — лимитка на уровне EMA.
Стоп — за ближайшим настоящим недельным хаем/лоем (из тех же недельных свечей),
лежащим дальше входа: реальная историческая точка разворота, а не производная
от EMA и не внутридневной шум (который во время самого пампа/дампа — просто
верхушка/подошва текущего движения, а не структура).
"""
import numpy as np

from ema_trend import calc_ema

WEEKLY_INTERVAL = "1w"
WEEKLY_LIMIT = 100
EMA_PERIODS = [7, 14, 28]
STOP_BUFFER_PCT = 2.0
TAKE_PROFIT_PCT = 3.0

# Санитарный потолок на дистанцию стоп-вход. Ближайший реальный недельный хай/лой
# дальше входа не всегда рядом — у молодых пар может не быть истории между входом
# и последним уровнем, тогда "ближайший" реальный уровень оказывается на другом конце
# всей доступной истории (проверено на реальном сигнале BANK/USDT: стоп улетел на 54%
# от входа при тейке 3%, риск/прибыль 18:1). Если дистанция больше кратности тейка —
# считаем структуру нерелевантной и не шлём сигнал, как и при полном отсутствии уровня.
STOP_MAX_DISTANCE_FACTOR = 3

# calc_ema() сажает EMA на SMA первых `period` свечей, дальше досчитывает по остатку —
# без разгона минимум в EMA_WARMUP_FACTOR раз больше периода значение остаётся смещено
# к затравке (проверено на реальных данных: EMA28 по 40 свечам расходился с TradingView
# на ~17%, по 84+ — совпадал).
EMA_WARMUP_FACTOR = 3


def calc_weekly_emas(closes: np.ndarray) -> dict[int, float]:
    """EMA по каждому периоду, для которого хватает недельных свечей на разгон (period * EMA_WARMUP_FACTOR).
    Недостающие пропускаются — не отдаём непрогретое (смещённое к затравке) значение."""
    return {
        period: calc_ema(closes, period)
        for period in EMA_PERIODS
        if len(closes) >= period * EMA_WARMUP_FACTOR
    }


def nearest_weekly_stop_level(weekly_candles: list[dict], entry: float, counter_direction: str) -> float | None:
    """Ближайший настоящий недельный хай (шорт) / лой (лонг), лежащий дальше входа.
    None — если в недельной истории нет свечи дальше входа (стоп поставить не за что)."""
    if counter_direction == "short":
        beyond = [c["high"] for c in weekly_candles if c["high"] > entry]
        return min(beyond) if beyond else None
    beyond = [c["low"] for c in weekly_candles if c["low"] < entry]
    return max(beyond) if beyond else None


def build_pullback_signal_for_period(counter_direction: str, weekly_candles: list[dict], entry_period: int) -> dict | None:
    """Вход на КОНКРЕТНОЙ недельной EMA (а не на ближайшей к цене) — используется как
    начальным контр-сигналом (через build_pullback_signal), так и трекингом при
    довыставлении на следующую EMA после пробоя текущей.
    counter_direction — направление самого контр-сигнала ("long"/"short"), не исходного памп/дамп.
    None — если период не прогрелся или для него нет структуры под стоп."""
    closes = np.array([c["close"] for c in weekly_candles])
    emas = calc_weekly_emas(closes)

    if entry_period not in emas:
        return None
    entry = emas[entry_period]

    stop_level = nearest_weekly_stop_level(weekly_candles, entry, counter_direction)
    if stop_level is None:
        return None

    if counter_direction == "long":
        stop = stop_level * (1 - STOP_BUFFER_PCT / 100)
        take = entry * (1 + TAKE_PROFIT_PCT / 100)
    else:
        stop = stop_level * (1 + STOP_BUFFER_PCT / 100)
        take = entry * (1 - TAKE_PROFIT_PCT / 100)

    stop_distance_pct = abs(entry - stop) / entry * 100
    if stop_distance_pct > TAKE_PROFIT_PCT * STOP_MAX_DISTANCE_FACTOR:
        return None

    return {
        "direction": counter_direction,
        "entry_period": entry_period,
        "entry": entry,
        "stop": stop,
        "take": take,
        "emas": emas,
    }


def build_pullback_signal(direction: str, price: float, weekly_candles: list[dict]) -> dict | None:
    """direction — направление ИСХОДНОГО памп/дамп сигнала ("long" на пампе, "short" на дампе).
    Контр-сигнал открывается в противоположную сторону, на ближайшей к цене недельной EMA.
    None — если недельной структуры (EMA для входа или реального хая/лоя дальше входа для
    стопа) не хватает."""
    closes = np.array([c["close"] for c in weekly_candles])
    emas = calc_weekly_emas(closes)

    counter_direction = "short" if direction == "long" else "long"

    if counter_direction == "long":
        side = {p: v for p, v in emas.items() if v < price}
    else:
        side = {p: v for p, v in emas.items() if v > price}

    if not side:
        return None

    pick_entry = max if counter_direction == "long" else min
    entry_period = pick_entry(side, key=lambda p: side[p])

    return build_pullback_signal_for_period(counter_direction, weekly_candles, entry_period)


def next_pullback_period(current_period: int) -> int | None:
    """Следующий период в цепочке EMA_PERIODS после текущего. None — если текущий последний."""
    idx = EMA_PERIODS.index(current_period)
    return EMA_PERIODS[idx + 1] if idx + 1 < len(EMA_PERIODS) else None


def evaluate_tracking(direction: str, entry: float, stop: float, daily_close: float) -> str:
    """Что делать с активным трекингом контр-сигнала по дневному закрытию:
    "advance" — закрытие прошло дальше входа (пора ждать отскока от следующей EMA),
    "invalidate" — закрытие пробило стоп (структура сломана, трекинг отменяем),
    "none" — ничего не изменилось, продолжаем ждать."""
    if direction == "long":
        if daily_close < stop:
            return "invalidate"
        if daily_close > entry:
            return "advance"
    else:
        if daily_close > stop:
            return "invalidate"
        if daily_close < entry:
            return "advance"
    return "none"
