from crypto_bot import fmt_caption


def test_fmt_caption_includes_trend_label_when_given():
    caption = fmt_caption(
        "BTC/USDT", "🚀 Pump", 6.5, 100.0, 106.5, 3.2, "1.2B",
        funding=0.05, trend_label="🟢 По тренду, у EMA20 (+0.3%)"
    )
    assert "Тренд: 🟢 По тренду, у EMA20 (+0.3%)" in caption


def test_fmt_caption_omits_trend_line_when_not_given():
    caption = fmt_caption("BTC/USDT", "🚀 Pump", 6.5, 100.0, 106.5, 3.2, "1.2B")
    assert "Тренд:" not in caption
