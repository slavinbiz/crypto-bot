import requests
import numpy as np
import crypto_bot


def test_fetch_pullback_signal_success(monkeypatch):
    closes = list(np.linspace(2.0, 1.0, 40))

    def fake_klines(symbol, interval, limit, timeout=10):
        return [{"close": c} for c in closes]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
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
    captured = {}

    def fake_klines(symbol, interval, limit, timeout=10):
        captured["interval"] = interval
        captured["limit"] = limit
        return [{"close": c} for c in np.linspace(2.0, 1.0, 40)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
    crypto_bot.fetch_pullback_signal("BTCUSDT", "short", price=1.2)

    assert captured["interval"] == "1w"
    assert captured["limit"] == 40
