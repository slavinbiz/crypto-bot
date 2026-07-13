from crypto_bot import classify_signal_outcome


def test_long_price_up_is_profit():
    pct, verdict = classify_signal_outcome("long", 100.0, 102.0)
    assert pct == 2.0
    assert verdict == "🟢 в плюс"


def test_long_price_down_is_loss():
    pct, verdict = classify_signal_outcome("long", 100.0, 98.0)
    assert pct == -2.0
    assert verdict == "🔴 в минус"


def test_short_price_down_is_profit():
    pct, verdict = classify_signal_outcome("short", 100.0, 98.0)
    assert pct == 2.0
    assert verdict == "🟢 в плюс"


def test_short_price_up_is_loss():
    pct, verdict = classify_signal_outcome("short", 100.0, 102.0)
    assert pct == -2.0
    assert verdict == "🔴 в минус"


def test_within_deadzone_is_breakeven():
    pct, verdict = classify_signal_outcome("long", 100.0, 100.3, deadzone_pct=0.5)
    assert verdict == "⚪ около входа (б/у)"


def test_at_exact_deadzone_boundary_is_breakeven():
    pct, verdict = classify_signal_outcome("long", 100.0, 100.5, deadzone_pct=0.5)
    assert verdict == "⚪ около входа (б/у)"
