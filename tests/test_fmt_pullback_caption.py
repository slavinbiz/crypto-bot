from crypto_bot import fmt_pullback_caption


def test_fmt_pullback_caption_long():
    pullback = {
        "direction": "long",
        "entry_period": 14,
        "entry": 0.00059855,
        "stop": 0.00047884,
        "take": 0.00061950,
        "stop_period": 7,
        "emas": {7: 0.00057735, 14: 0.00059855, 28: 0.00076255},
    }
    caption = fmt_pullback_caption("B3/USDT", pullback)

    assert "🟢 LONG" in caption
    assert "EMA14(W)" in caption
    assert "ВХОД:" in caption and "СТОП:" in caption and "ТЕЙК:" in caption
    assert "EMA7(W)" in caption and "EMA28(W)" in caption


def test_fmt_pullback_caption_short():
    pullback = {
        "direction": "short",
        "entry_period": 14,
        "entry": 1.9,
        "stop": 2.0,
        "take": 1.84,
        "stop_period": 7,
        "emas": {7: 2.0, 14: 1.9},
    }
    caption = fmt_pullback_caption("BTC/USDT", pullback)

    assert "🔴 SHORT" in caption
