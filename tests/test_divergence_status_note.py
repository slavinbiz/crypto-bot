from crypto_bot import divergence_status_note, has_rsi_divergence


def test_no_note_when_unchanged_no_divergence():
    assert divergence_status_note(False, False, 15) is None


def test_no_note_when_unchanged_has_divergence():
    assert divergence_status_note(True, True, 60) is None


def test_no_note_when_stale_data():
    assert divergence_status_note(False, None, 15) is None


def test_note_when_divergence_appears():
    note = divergence_status_note(False, True, 60)
    assert "появилась" in note
    assert "60м" in note
    assert "нет" in note  # что было при входе


def test_note_when_divergence_disappears():
    note = divergence_status_note(True, False, 240)
    assert "пропала" in note
    assert "240м" in note
    assert "есть" in note  # что было при входе


def test_has_rsi_divergence_catches_second_peak_after_signal_moment():
    """Регресс для разбора VTHOUSDT 12.09.2026: в момент разворотной свечи сразу после
    пампа второго (более высокого) пика ещё нет — дивергенция не видна. Но как только
    цена доходит до него — has_rsi_divergence должна её поймать (это и проверяет
    post-signal перепроверка в verify_signal)."""
    base = [100.0] * 20
    pump = [102, 108, 116, 126, 138]
    plateau = [140, 137, 143, 139, 145, 141, 147]  # каждый следующий хай выше, RSI по ним будет ниже
    closes = base + pump + plateau

    candles_at_signal = [{"close": c} for c in closes[:27]]   # свеча сразу после первого пика (пин-бар)
    candles_later      = [{"close": c} for c in closes]        # после того, как сложился второй, более высокий пик

    assert bool(has_rsi_divergence(candles_at_signal, bearish=True)) is False
    assert bool(has_rsi_divergence(candles_later, bearish=True)) is True
