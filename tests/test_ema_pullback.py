import numpy as np
import pytest

from ema_pullback import calc_weekly_emas, build_pullback_signal, EMA_PERIODS, EMA_WARMUP_FACTOR, has_recent_spike, SPIKE_LOOKBACK_CANDLES, SPIKE_BODY_PCT_THRESHOLD


def make_candles(n: int, start: float = 1.0, step: float = 0.01) -> list[dict]:
    return [{"close": start + i * step} for i in range(n)]


def make_weekly_candles(closes: list[float], overrides: dict[int, dict] | None = None) -> list[dict]:
    """closes — для расчёта EMA; overrides — {индекс: {"high": v, "low": v, "open": v}} для конкретных
    недельных свечей. По умолчанию high/low выставлены так, чтобы никогда не попасть в поиск
    ближайшего уровня дальше входа (короткого high << любого входа, длинного low >> любого входа) —
    в тесте "видны" только явно заданные overrides. По умолчанию open == close (тело свечи 0%,
    не триггерит гейт свежего спайка)."""
    overrides = overrides or {}
    candles = []
    for i, c in enumerate(closes):
        ov = overrides.get(i, {})
        candles.append({
            "open": ov.get("open", c),
            "close": c,
            "high": ov.get("high", -1e9),
            "low": ov.get("low", 1e9),
        })
    return candles


def test_calc_weekly_emas_skips_periods_without_enough_warmup():
    # 30 свечей: хватает на разгон EMA7 (21) и EMA14 (42? нет — 30<42, тоже недостаточно)
    # так что при 30 свечах остаётся только EMA7 (нужно >= 7*factor = 21)
    closes = np.array([c["close"] for c in make_candles(30)])
    emas = calc_weekly_emas(closes)
    assert set(emas.keys()) == {7}


def test_calc_weekly_emas_all_periods_with_enough_warmup():
    # 28 * EMA_WARMUP_FACTOR свечей хватает на разгон всех трёх периодов
    closes = np.array([c["close"] for c in make_candles(28 * EMA_WARMUP_FACTOR)])
    emas = calc_weekly_emas(closes)
    assert set(emas.keys()) == set(EMA_PERIODS)


def test_build_pullback_signal_returns_none_with_too_few_candles():
    candles = make_weekly_candles([c["close"] for c in make_candles(10)])
    assert build_pullback_signal("short", price=1.5, weekly_candles=candles) is None


def test_build_pullback_signal_long_counter_to_dump():
    # Падающий ряд, 100 недельных свечей (как в проде): EMA7≈1.030, EMA14≈1.066, EMA28≈1.136.
    # Цена между EMA14 и EMA28 — вход EMA14
    closes = list(np.linspace(2.0, 1.0, 100))
    candles = make_weekly_candles(closes, {
        0: {"low": 1.02},
        1: {"low": 1.05},
        2: {"low": 1.03},
    })
    price = 1.10

    result = build_pullback_signal("short", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "long"
    assert result["entry_period"] == 14
    assert result["entry"] < price
    assert result["stop"] < result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 1.03)
    # стоп = ближайший (наибольший) недельный лой ДАЛЬШЕ входа минус буфер 2%
    assert result["stop"] == pytest.approx(1.05 * 0.98)


def test_build_pullback_signal_short_counter_to_pump():
    # Растущий ряд, 100 свечей: EMA7≈1.970, EMA14≈1.934, EMA28≈1.864.
    # Цена между EMA28 и EMA14 — вход EMA14
    closes = list(np.linspace(1.0, 2.0, 100))
    candles = make_weekly_candles(closes, {
        0: {"high": 1.94},
        1: {"high": 1.95},
        2: {"high": 1.96},
    })
    price = 1.90

    result = build_pullback_signal("long", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "short"
    assert result["entry_period"] == 14
    assert result["entry"] > price
    assert result["stop"] > result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 0.97)
    # стоп = ближайший (наименьший) недельный хай ДАЛЬШЕ входа плюс буфер 2%
    assert result["stop"] == pytest.approx(1.94 * 1.02)


def test_build_pullback_signal_works_with_only_one_ema_if_real_high_exists():
    # Реальный кейс PROM/USDT: только EMA28 выше цены (сильный устойчивый рост,
    # EMA7/14 уже ниже цены) — раньше гейт "минимум 2 EMA" резал такой сигнал.
    # Но если в недельной истории есть настоящий хай дальше входа (EMA28) —
    # стоп есть за что поставить, сигнал должен пройти.
    closes = [5.0] * 30 + list(np.linspace(5.0, 2.0, 70))  # EMA7≈2.13, EMA14≈2.28, EMA28≈2.58
    candles = make_weekly_candles(closes, {5: {"high": 2.7}})  # реальный хай чуть выше EMA28
    price = 2.30  # между EMA14 и EMA28 — только EMA28 выше цены

    result = build_pullback_signal("long", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "short"
    assert result["entry_period"] == 28
    assert result["stop"] == pytest.approx(2.7 * 1.02)


def test_build_pullback_signal_none_when_stop_too_far_from_entry():
    # Реальный кейс BANK/USDT: молодая пара, между входом и ближайшим реальным лоем
    # нет истории — "ближайший" реальный уровень оказывается на другом конце всей
    # доступной истории (54% от входа при тейке 3%, риск/прибыль 18:1). Такую
    # структуру считаем нерелевантной и сигнал не шлём.
    closes = list(np.linspace(2.0, 1.0, 100))
    candles = make_weekly_candles(closes, {0: {"low": 0.4}})  # единственный лой — далеко от входа
    price = 1.10

    result = build_pullback_signal("short", price=price, weekly_candles=candles)
    assert result is None


def test_build_pullback_signal_none_when_no_real_level_beyond_entry():
    # Тот же вход (EMA14), что и в test_..._long_counter_to_dump, но в истории
    # нет ни одной недельной свечи с лоем ниже входа — стоп поставить не за что.
    closes = list(np.linspace(2.0, 1.0, 100))
    candles = make_weekly_candles(closes)  # без overrides — все low = 1e9 (никогда не ниже входа)
    price = 1.10

    result = build_pullback_signal("short", price=price, weekly_candles=candles)
    assert result is None


def test_has_recent_spike_true_when_last_candle_body_exceeds_threshold():
    # Реальный кейс SYN/USDT: последняя свеча — резкий обвал с 1.0 до 0.15 (тело 85%)
    candles = make_weekly_candles([0.2] * 10, overrides={9: {"open": 1.0}})
    candles[9]["close"] = 0.15
    assert has_recent_spike(candles) is True


def test_has_recent_spike_true_when_second_to_last_candle_is_the_spike():
    # Свеча пампа — предпоследняя (open=0.03, close=1.0, тело >3000%), последняя — обычная
    candles = make_weekly_candles([0.2] * 10, overrides={8: {"open": 0.03}})
    candles[8]["close"] = 1.0
    assert has_recent_spike(candles) is True


def test_has_recent_spike_false_when_spike_is_old():
    # Тот же скачок, но 3 свечи назад — вне окна SPIKE_LOOKBACK_CANDLES (2)
    candles = make_weekly_candles([0.2] * 10, overrides={6: {"open": 0.03}})
    candles[6]["close"] = 1.0
    assert has_recent_spike(candles) is False


def test_has_recent_spike_false_for_smooth_trend():
    # Плавный тренд — open == close по умолчанию, тело 0%
    candles = make_weekly_candles(list(np.linspace(1.0, 2.0, 10)))
    assert has_recent_spike(candles) is False


def test_has_recent_spike_false_exactly_at_threshold():
    # Тело ровно на пороге (не строго больше) — не считается спайком
    candles = make_weekly_candles([0.2] * 10, overrides={9: {"open": 1.0}})
    candles[9]["close"] = 0.5  # тело = 50.0%, ровно порог
    assert has_recent_spike(candles) is False


def test_build_pullback_signal_none_when_recent_spike():
    # SYN-подобный сценарий: долгий даунтренд, затем на последней неделе резкий памп-обвал —
    # даже если формально EMA и стоп нашлись бы, гейт спайка должен отменить сигнал целиком.
    closes = list(np.linspace(2.0, 1.0, 99)) + [1.0]
    candles = make_weekly_candles(closes, {
        0: {"low": 0.5},
        98: {"open": 0.03},
    })
    candles[98]["close"] = 1.0  # последняя свеча: open=0.03, close=1.0 — тело >3000%

    result = build_pullback_signal("short", price=1.10, weekly_candles=candles)
    assert result is None


def test_make_weekly_candles_default_open_equals_close():
    candles = make_weekly_candles([1.0, 2.0, 3.0])
    assert all(c["open"] == c["close"] for c in candles)


def test_make_weekly_candles_open_override():
    candles = make_weekly_candles([1.0, 2.0, 3.0], overrides={1: {"open": 0.5}})
    assert candles[1]["open"] == 0.5
    assert candles[0]["open"] == candles[0]["close"]
