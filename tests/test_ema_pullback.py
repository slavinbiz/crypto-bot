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
    candles = make_weekly_candles([c["close"] for c in make_candles(10)])
    assert build_pullback_signal("short", price=1.5, weekly_candles=candles) is None


def make_weekly_candles(closes: list[float], overrides: dict[int, dict] | None = None) -> list[dict]:
    """closes — для расчёта EMA; overrides — {индекс: {"high": v, "low": v}} для конкретных
    недельных свечей. По умолчанию high/low выставлены так, чтобы никогда не попасть в поиск
    ближайшего уровня дальше входа (короткого high << любого входа, длинного low >> любого входа) —
    в тесте "видны" только явно заданные overrides."""
    overrides = overrides or {}
    candles = []
    for i, c in enumerate(closes):
        ov = overrides.get(i, {})
        candles.append({"close": c, "high": ov.get("high", -1e9), "low": ov.get("low", 1e9)})
    return candles


def test_build_pullback_signal_long_counter_to_dump():
    # Падающий ряд: EMA7≈1.08, EMA14≈1.17, EMA28≈1.35 — берём цену между EMA14 и EMA28,
    # вход — EMA14 (1.1667)
    closes = list(np.linspace(2.0, 1.0, 40))
    candles = make_weekly_candles(closes, {
        0: {"low": 1.05},
        1: {"low": 1.10},
        2: {"low": 1.08},
    })
    price = 1.2

    result = build_pullback_signal("short", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "long"
    assert result["entry"] < price
    assert result["stop"] < result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 1.03)
    # стоп = ближайший (наибольший) недельный лой ДАЛЬШЕ входа минус буфер 2%
    assert result["stop"] == pytest.approx(1.10 * 0.98)


def test_build_pullback_signal_short_counter_to_pump():
    # Растущий ряд: EMA7≈1.92, EMA14≈1.83, EMA28≈1.65 — берём цену между EMA28 и EMA14,
    # вход — EMA14 (1.8333)
    closes = list(np.linspace(1.0, 2.0, 40))
    candles = make_weekly_candles(closes, {
        0: {"high": 1.90},
        1: {"high": 1.95},
        2: {"high": 1.92},
    })
    price = 1.7

    result = build_pullback_signal("long", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "short"
    assert result["entry"] > price
    assert result["stop"] > result["entry"]
    assert result["take"] == pytest.approx(result["entry"] * 0.97)
    # стоп = ближайший (наименьший) недельный хай ДАЛЬШЕ входа плюс буфер 2%
    assert result["stop"] == pytest.approx(1.90 * 1.02)


def test_build_pullback_signal_works_with_only_one_ema_if_real_high_exists():
    # Реальный кейс PROM/USDT: только EMA28 выше цены (сильный устойчивый рост,
    # EMA7/14 уже ниже цены) — раньше гейт "минимум 2 EMA" резал такой сигнал.
    # Но если в недельной истории есть настоящий хай дальше входа (EMA28) —
    # стоп есть за что поставить, сигнал должен пройти.
    closes = [5.0] * 12 + list(np.linspace(5.0, 2.0, 28))
    candles = make_weekly_candles(closes, {3: {"high": 3.5}})  # реальный хай чуть выше EMA28
    price = 2.71  # между EMA14(~2.71) и EMA28(~3.37) — только EMA28 выше цены

    result = build_pullback_signal("long", price=price, weekly_candles=candles)

    assert result is not None
    assert result["direction"] == "short"
    assert result["entry"] == pytest.approx(3.3686943974321304)
    assert result["stop"] == pytest.approx(3.5 * 1.02)


def test_build_pullback_signal_none_when_no_real_level_beyond_entry():
    # Тот же вход (EMA14), что и в test_..._long_counter_to_dump, но в истории
    # нет ни одной недельной свечи с лоем ниже входа — стоп поставить не за что.
    closes = list(np.linspace(2.0, 1.0, 40))
    candles = make_weekly_candles(closes)  # без overrides — все low = 1e9 (никогда не ниже входа)
    price = 1.2

    result = build_pullback_signal("short", price=price, weekly_candles=candles)
    assert result is None
