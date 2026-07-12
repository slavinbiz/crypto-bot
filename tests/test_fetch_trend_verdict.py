import requests
import crypto_bot


def test_fetch_trend_verdict_success(monkeypatch):
    def fake_klines(symbol, interval, limit):
        if interval == "1h":
            return [{"close": 100.0 + i * 0.5} for i in range(limit)]
        return [{"close": 100.0} for _ in range(limit)]

    monkeypatch.setattr(crypto_bot, "get_klines", fake_klines)
    result = crypto_bot.fetch_trend_verdict("BTCUSDT", "long", 100.0)
    assert result["verdict"] == "trend_at_ema"


def test_fetch_trend_verdict_returns_unknown_on_error(monkeypatch):
    def broken_klines(symbol, interval, limit):
        raise requests.exceptions.Timeout("boom")

    monkeypatch.setattr(crypto_bot, "get_klines", broken_klines)
    result = crypto_bot.fetch_trend_verdict("BTCUSDT", "long", 100.0)
    assert result["verdict"] == "unknown"
    assert result["label"] == "⚪ Не удалось проверить"
    assert result["distance_pct"] is None
