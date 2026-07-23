import requests
import numpy as np
import pytest
import crypto_bot
import ema_pullback


def fake_klines(daily_close, weekly_closes=None, weekly_overrides=None):
    """daily_close — close закрытой дневной свечи (индекс -2 в ответе klines,
    последняя свеча в ответе всегда текущая/незакрытая). weekly_closes/overrides —
    как в test_fetch_pullback_signal.py, нужны только для сценария advance."""
    weekly_overrides = weekly_overrides or {}
    weekly_closes = weekly_closes or []

    def fake(symbol, interval, limit, timeout=10):
        if interval == "1d":
            return [{"close": daily_close}, {"close": 0.0}]
        assert interval == ema_pullback.WEEKLY_INTERVAL
        candles = []
        for i, c in enumerate(weekly_closes):
            ov = weekly_overrides.get(i, {})
            candles.append({"close": c, "high": ov.get("high", -1e9), "low": ov.get("low", 1e9)})
        return candles
    return fake


def test_fetch_tracking_update_none_between_stop_and_entry(monkeypatch):
    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines(daily_close=0.95))
    result = crypto_bot.fetch_tracking_update("BTCUSDT", "long", entry_period=7, entry=1.0, stop=0.9)
    assert result == {"decision": "none", "daily_close": 0.95}


def test_fetch_tracking_update_invalidate_when_close_breaks_stop(monkeypatch):
    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines(daily_close=0.85))
    result = crypto_bot.fetch_tracking_update("BTCUSDT", "long", entry_period=7, entry=1.0, stop=0.9)
    assert result == {"decision": "invalidate", "daily_close": 0.85}


def test_fetch_tracking_update_done_when_already_at_last_period(monkeypatch):
    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines(daily_close=1.10))
    result = crypto_bot.fetch_tracking_update("BTCUSDT", "long", entry_period=28, entry=1.0, stop=0.9)
    assert result == {"decision": "done"}


def test_fetch_tracking_update_done_when_next_period_lacks_structure(monkeypatch):
    # только 10 недельных свечей — не хватит на разгон EMA14 (нужно >= 14*3=42)
    monkeypatch.setattr(
        crypto_bot, "get_klines",
        fake_klines(daily_close=1.10, weekly_closes=list(np.linspace(2.0, 1.0, 10)))
    )
    result = crypto_bot.fetch_tracking_update("BTCUSDT", "long", entry_period=7, entry=1.0, stop=0.9)
    assert result == {"decision": "done"}


def test_fetch_tracking_update_advance_returns_next_pullback(monkeypatch):
    weekly_closes = list(np.linspace(2.0, 1.0, 100))
    monkeypatch.setattr(
        crypto_bot, "get_klines",
        fake_klines(
            daily_close=1.10, weekly_closes=weekly_closes,
            weekly_overrides={0: {"low": 1.05}, 1: {"low": 1.02}}
        )
    )
    result = crypto_bot.fetch_tracking_update("BTCUSDT", "long", entry_period=7, entry=1.0, stop=0.9)
    assert result["decision"] == "advance"
    assert result["pullback"]["entry_period"] == 14
    assert result["pullback"]["stop"] == pytest.approx(1.05 * 0.98)


def test_fetch_tracking_update_returns_none_on_daily_klines_error(monkeypatch):
    def broken(symbol, interval, limit, timeout=10):
        raise requests.exceptions.Timeout("boom")
    monkeypatch.setattr(crypto_bot, "get_klines", broken)
    result = crypto_bot.fetch_tracking_update("BTCUSDT", "long", entry_period=7, entry=1.0, stop=0.9)
    assert result is None


def test_fetch_tracking_update_returns_none_when_fewer_than_two_daily_candles(monkeypatch):
    def fake(symbol, interval, limit, timeout=10):
        return [{"close": 1.0}]
    monkeypatch.setattr(crypto_bot, "get_klines", fake)
    result = crypto_bot.fetch_tracking_update("BTCUSDT", "long", entry_period=7, entry=1.0, stop=0.9)
    assert result is None
