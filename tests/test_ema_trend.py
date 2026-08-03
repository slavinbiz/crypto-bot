import numpy as np
import pytest
import ema_trend
from ema_trend import calc_ema, get_trend_verdict


def test_trend_limit_gives_converged_ema200():
    """TREND_LIMIT должен давать calc_ema достаточно "разгона" после затравки —
    иначе EMA200 остаётся смесью в основном из SMA(200), а не настоящим EMA200
    (баг: при TREND_LIMIT=210 затравка после 10 шагов сохраняла ~90% веса,
    из-за чего 1H-тренд иногда определялся неверно на волатильных альтах —
    см. дневник 2026-08-03)."""
    period = ema_trend.EMA_TREND_SLOW
    warmup_steps = ema_trend.TREND_LIMIT - period
    alpha = 2 / (period + 1)
    seed_residual_weight = (1 - alpha) ** warmup_steps
    assert seed_residual_weight < 0.01


def test_pullback_limit_gives_converged_ema20():
    """Тот же баг конвергенции calc_ema, что и у TREND_LIMIT, но для EMA20 на 15m
    (см. дневник 2026-08-03) — при PULLBACK_LIMIT=30 затравка сохраняла ~37% веса."""
    period = ema_trend.EMA_PULLBACK
    warmup_steps = ema_trend.PULLBACK_LIMIT - period
    alpha = 2 / (period + 1)
    seed_residual_weight = (1 - alpha) ** warmup_steps
    assert seed_residual_weight < 0.01


def test_calc_ema_basic():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    # seed = mean(10,11,12) = 11.0; alpha = 2/(3+1) = 0.5
    # шаг1: 0.5*13 + 0.5*11.0 = 12.0
    # шаг2: 0.5*14 + 0.5*12.0 = 13.0
    result = calc_ema(closes, period=3)
    assert result == pytest.approx(13.0)


def test_calc_ema_insufficient_data_raises():
    closes = np.array([10.0, 11.0])
    with pytest.raises(ValueError):
        calc_ema(closes, period=3)


def _uptrend_candles(n=210, start=100.0, step=0.5):
    return [{"close": start + i * step} for i in range(n)]


def _flat_candles(n=30, price=100.0):
    return [{"close": price} for _ in range(n)]


def test_verdict_trend_at_ema_for_long():
    trend = _uptrend_candles()
    pullback = _flat_candles(price=100.0)
    result = get_trend_verdict("long", price=100.0, trend_candles=trend, pullback_candles=pullback)
    assert result["verdict"] == "trend_at_ema"
    assert result["distance_pct"] == pytest.approx(0.0)


def test_verdict_trend_far_ema_for_long():
    trend = _uptrend_candles()
    pullback = _flat_candles(price=100.0)
    result = get_trend_verdict("long", price=110.0, trend_candles=trend, pullback_candles=pullback)
    assert result["verdict"] == "trend_far_ema"


def test_verdict_against_trend_for_short_in_uptrend():
    trend = _uptrend_candles()
    pullback = _flat_candles(price=100.0)
    result = get_trend_verdict("short", price=100.0, trend_candles=trend, pullback_candles=pullback)
    assert result["verdict"] == "against_trend"
