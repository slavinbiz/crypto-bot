import requests
import numpy as np
import crypto_bot
import ema_pullback


def fake_klines(weekly_closes):
    def fake(symbol, interval, limit, timeout=10):
        assert interval == ema_pullback.WEEKLY_INTERVAL
        return [{"close": c, "high": c, "low": c} for c in weekly_closes]
    return fake


def test_fetch_pullback_signal_success(monkeypatch):
    closes = list(np.linspace(2.0, 1.0, 40))

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines(closes))
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
    def fake(symbol, interval, limit, timeout=10):
        return [{"close": 1.0, "high": 1.0, "low": 1.0} for _ in range(3)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake)
    result = crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    assert result is None


def test_fetch_pullback_signal_uses_weekly_interval(monkeypatch):
    calls = []

    def fake(symbol, interval, limit, timeout=10):
        calls.append((interval, limit))
        return [{"close": c, "high": c, "low": c} for c in np.linspace(2.0, 1.0, 40)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake)
    crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    assert ("1w", 40) in calls
    assert len(calls) == 1  # больше не дёргаем отдельный внутридневной запрос под стоп
