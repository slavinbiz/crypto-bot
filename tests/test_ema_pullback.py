import numpy as np
import pytest

from ema_pullback import calc_weekly_emas, build_pullback_signal, EMA_PERIODS


def make_candles(n: int, start: float = 1.0, step: float = 0.01) -> list[dict]:
    return [{"close": start + i * step} for i in range(n)]


def test_calc_weekly_emas_skips_periods_without_enough_data():
    closes = np.array([c["close"] for c in make_candles(20)])
    emas = calc_weekly_emas(closes)
    assert set(emas.keys()) == {7, 14}  # 28 не хватает свечей


def test_calc_weekly_emas_all_periods_with_enough_data():
    closes = np.array([c["close"] for c in make_candles(40)])
    emas = calc_weekly_emas(closes)
    assert set(emas.keys()) == set(EMA_PERIODS)


def test_build_pullback_signal_returns_none_with_too_few_candles():
    candles = make_candles(10)
    assert build_pullback_signal("short", price=1.5, weekly_candles=candles, stop_candles=[]) is None


def make_stop_candles(lows_highs: list[tuple[float, float]]) -> list[dict]:
    return [{"low": lo, "high": hi} for lo, hi in lows_highs]


def test_build_pullback_signal_long_counter_to_dump():
    # Падающий ряд: EMA7≈1.08, EMA14≈1.17, EMA28≈1.35 — берём цену между EMA14 и EMA28,
    # тогда снизу от цены остаются EMA7 и EMA14 (support-уровни, гейт "минимум 2 EMA")
    closes = list(np.linspace(2.0, 1.0, 40))
    candles = [{"close": c} for c in closes]
    price = 1.2
    stop_candles = make_stop_candles([(1.15, 1.25), (1.10, 1.22), (1.18, 1.28)])  # ближайший лой 1.10

    result = build_pullback_signal("short", price=price, weekly_candles=candles, stop_candles=stop_candles)

    assert result is not None
    assert result["direction"] == "long"
    assert result["entry"] < price
    assert result["stop"] < result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 1.03)
    # стоп = ближайший лой из lookback-свечей минус буфер 2%
    assert result["stop"] == pytest.approx(1.10 * 0.98)


def test_build_pullback_signal_short_counter_to_pump():
    # Растущий ряд: EMA7≈1.92, EMA14≈1.83, EMA28≈1.65 — берём цену между EMA28 и EMA14,
    # тогда сверху от цены остаются EMA7 и EMA14 (resistance-уровни, гейт "минимум 2 EMA")
    closes = list(np.linspace(1.0, 2.0, 40))
    candles = [{"close": c} for c in closes]
    price = 1.7
    stop_candles = make_stop_candles([(1.65, 1.75), (1.68, 1.80), (1.66, 1.72)])  # ближайший хай 1.80

    result = build_pullback_signal("long", price=price, weekly_candles=candles, stop_candles=stop_candles)

    assert result is not None
    assert result["direction"] == "short"
    assert result["entry"] > price
    assert result["stop"] > result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 0.97)
    # стоп = ближайший хай из lookback-свечей плюс буфер 2%
    assert result["stop"] == pytest.approx(1.80 * 1.02)


def test_build_pullback_signal_none_when_only_one_ema_on_pullback_side():
    # Цена между EMA7 и EMA14 — только одна EMA (EMA7) ниже цены, стоп поставить не за что
    closes = list(np.linspace(2.0, 1.0, 20))  # хватает только на EMA7/EMA14
    candles = [{"close": c} for c in closes]

    # искусственно: цена чуть выше EMA14, но ниже несуществующей EMA28 —
    # эмулируем сценарий "только одна EMA снизу" через короткий ряд с ценой между уровнями
    from ema_pullback import calc_weekly_emas
    closes_arr = np.array(closes)
    emas = calc_weekly_emas(closes_arr)
    price = emas[7] + (emas[14] - emas[7]) / 2  # между EMA7 и EMA14 -> только EMA7 снизу

    result = build_pullback_signal("short", price=price, weekly_candles=candles, stop_candles=[])
    assert result is None
