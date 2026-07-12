import requests
import crypto_bot


def test_fetch_trend_verdict_success(monkeypatch):
    def fake_klines(symbol, interval, limit, timeout=10):
        if interval == "1h":
            return [{"close": 100.0 + i * 0.5} for i in range(limit)]
        return [{"close": 100.0} for _ in range(limit)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
    result = crypto_bot.fetch_trend_verdict("BTCUSDT", "long", 100.0)
    assert result["verdict"] == "trend_at_ema"


def test_fetch_trend_verdict_returns_unknown_on_error(monkeypatch):
    def broken_klines(symbol, interval, limit, timeout=10):
        raise requests.exceptions.Timeout("boom")

    monkeypatch.setattr(crypto_bot, "get_klines", broken_klines)
    result = crypto_bot.fetch_trend_verdict("BTCUSDT", "long", 100.0)
    assert result["verdict"] == "unknown"
    assert result["label"] == "⚪ Не удалось проверить"
    assert result["distance_pct"] is None


def test_fetch_trend_verdict_uses_short_timeout(monkeypatch):
    """trend-check запросы не должны стопорить event loop надолго — таймаут 4с, а не дефолтные 10с."""
    captured_timeouts = []

    def fake_klines(symbol, interval, limit, timeout=10):
        captured_timeouts.append(timeout)
        if interval == "1h":
            return [{"close": 100.0 + i * 0.5} for i in range(limit)]
        return [{"close": 100.0} for _ in range(limit)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
    crypto_bot.fetch_trend_verdict("BTCUSDT", "long", 100.0)

    assert len(captured_timeouts) == 2
    assert captured_timeouts == [4, 4]


def test_get_klines_defaults_timeout_to_ten(monkeypatch):
    """Остальные вызовы get_klines (например, get_pair_age_days) не должны пострадать от нового параметра."""
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def fake_get(url, params=None, timeout=None):
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(crypto_bot.requests, "get", fake_get)
    crypto_bot.get_klines("BTCUSDT", "1m", 5)

    assert captured["timeout"] == 10
