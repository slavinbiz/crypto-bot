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
    assert build_pullback_signal("short", price=1.5, weekly_candles=candles) is None


def test_build_pullback_signal_long_counter_to_dump():
    # Падающий ряд: EMA7≈1.08, EMA14≈1.17, EMA28≈1.35 — берём цену между EMA14 и EMA28,
    # тогда снизу от цены остаются EMA7 и EMA14 (support-уровни)
    closes = list(np.linspace(2.0, 1.0, 40))
    candles = [{"close": c} for c in closes]
    price = 1.2

    result = build_pullback_signal("short", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "long"
    assert result["entry"] < price
    assert result["stop"] < result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 1.03)
    # стоп = дальняя EMA минус буфер 2%
    stop_base = result["emas"][result["stop_period"]]
    assert result["stop"] == pytest.approx(stop_base * 0.98)


def test_build_pullback_signal_short_counter_to_pump():
    # Растущий ряд: EMA7≈1.92, EMA14≈1.83, EMA28≈1.65 — берём цену между EMA28 и EMA14,
    # тогда сверху от цены остаются EMA7 и EMA14 (resistance-уровни)
    closes = list(np.linspace(1.0, 2.0, 40))
    candles = [{"close": c} for c in closes]
    price = 1.7

    result = build_pullback_signal("long", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "short"
    assert result["entry"] > price
    assert result["stop"] > result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 0.97)
    stop_base = result["emas"][result["stop_period"]]
    assert result["stop"] == pytest.approx(stop_base * 1.02)


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

    result = build_pullback_signal("short", price=price, weekly_candles=candles)
    assert result is None
