import requests
import numpy as np
import crypto_bot
import ema_pullback


def fake_klines_by_interval(weekly_closes, stop_low=1.10, stop_high=1.30):
    def fake_klines(symbol, interval, limit, timeout=10):
        if interval == ema_pullback.WEEKLY_INTERVAL:
            return [{"close": c} for c in weekly_closes]
        return [{"low": stop_low, "high": stop_high} for _ in range(limit)]
    return fake_klines


def test_fetch_pullback_signal_success(monkeypatch):
    closes = list(np.linspace(2.0, 1.0, 40))

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines_by_interval(closes))
    result = crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    assert result is not None
    assert result["direction"] == "long"


def test_fetch_pullback_signal_returns_none_on_error(monkeypatch):
    def broken_klines(symbol, interval, limit, timeout=10):
        raise requests.exceptions.Timeout("boom")

    monkeypatch.setattr(crypto_bot, "get_klines", broken_klines)
    result = crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    assert result is None


def test_fetch_pullback_signal_returns_none_when_not_enough_structure(monkeypatch):
    def fake_klines(symbol, interval, limit, timeout=10):
        return [{"close": 1.0} for _ in range(3)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
    result = crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    assert result is None


def test_fetch_pullback_signal_uses_weekly_interval(monkeypatch):
    calls = []

    def fake_klines(symbol, interval, limit, timeout=10):
        calls.append((interval, limit))
        if interval == ema_pullback.WEEKLY_INTERVAL:
            return [{"close": c} for c in np.linspace(2.0, 1.0, 40)]
        return [{"low": 1.10, "high": 1.30} for _ in range(limit)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
    crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    assert ("1w", 40) in calls


def test_fetch_pullback_signal_uses_stop_lookback_interval(monkeypatch):
    calls = []

    def fake_klines(symbol, interval, limit, timeout=10):
        calls.append((interval, limit))
        if interval == ema_pullback.WEEKLY_INTERVAL:
            return [{"close": c} for c in np.linspace(2.0, 1.0, 40)]
        return [{"low": 1.10, "high": 1.30} for _ in range(limit)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
    crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    stop_interval = ema_pullback.STOP_LOOKBACK_INTERVAL
    stop_limit = ema_pullback.STOP_LOOKBACK_CANDLES[stop_interval]
    assert (stop_interval, stop_limit) in calls
