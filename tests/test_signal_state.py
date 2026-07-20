import numpy as np
import crypto_bot


def make_flat_then_ramp_candles(flat_price: float, end_price: float, flat_n: int = 5, ramp_n: int = 60, vol: float = 100.0) -> list[dict]:
    """flat_n свечей на flat_price, затем ramp_n свечей линейно до end_price.
    Объём одинаковый на каждой свече — IIV всегда ~1.0, всплеска объёма нет."""
    flat = [{"close": flat_price, "vol_buy": vol / 2, "vol_sell": vol / 2} for _ in range(flat_n)]
    ramp_prices = np.linspace(flat_price, end_price, ramp_n + 1)[1:]
    ramp = [{"close": p, "vol_buy": vol / 2, "vol_sell": vol / 2} for p in ramp_prices]
    return flat + ramp


def test_grinding_pump_triggers_without_volume_spike():
    # +15% растянуто на 60 свечей без всплеска объёма — "тягучий" памп вроде ALICE 20.07
    state = crypto_bot.SignalState()
    candles = make_flat_then_ramp_candles(flat_price=1.0, end_price=1.15)

    triggered, desc, rsi = state.check(candles)

    assert triggered
    assert "ПАМП" in desc


def test_moderate_pump_without_volume_spike_does_not_trigger():
    # +8% — выше базового порога 6%, но ниже "сильного" 12% (2x) — без всплеска объёма сигнала быть не должно
    state = crypto_bot.SignalState()
    candles = make_flat_then_ramp_candles(flat_price=1.0, end_price=1.08)

    triggered, desc, rsi = state.check(candles)

    assert not triggered
