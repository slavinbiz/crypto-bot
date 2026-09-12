from crypto_bot import find_reversal_pattern


def test_single_pin_bar_entry_open_is_its_own_open():
    """Одиночный пин-бар — entry_open берём прямо из его собственного open."""
    candles = [
        {"open": 100, "high": 101, "low": 99, "close": 100.2},
        {"open": 105, "high": 115, "low": 104.8, "close": 105.2},  # длинный верхний хвост, тело маленькое
    ]
    pattern = find_reversal_pattern(candles, since_signal=[candles[-1]], bearish=True)
    assert pattern is not None
    assert pattern["kind"] == "пин-бар"
    assert pattern["entry_open"] == 105


def test_engulfing_entry_open_is_its_own_open():
    prev = {"open": 100, "high": 103, "low": 99, "close": 102}   # зелёная
    cur  = {"open": 103, "high": 104, "low": 97,  "close": 98}   # красная, перекрывает тело
    pattern = find_reversal_pattern([prev, cur], since_signal=[cur], bearish=True)
    assert pattern is not None
    assert pattern["kind"] == "поглощение"
    assert pattern["entry_open"] == 103


def test_combined_pin_bar_entry_open_is_last_real_candle_open_not_peak():
    """Регресс для разбора 12.09.2026: у схлопнутого пин-бара combo["open"] — это open
    ПЕРВОЙ (пиковой) свечи, которая всё ещё часть самого рывка — использовать её как
    цену входа нельзя (проверено на истории: R:R по ней либо огромный, либо отрицательный,
    не отражает реальную точку входа). entry_open должен быть open ПОСЛЕДНЕЙ свечи паттерна."""
    peak  = {"open": 100, "high": 112, "low": 100, "close": 110}   # свеча пика рывка
    tail  = {"open": 111, "high": 113, "low": 98,  "close": 99}    # свеча, формирующая хвост отказа
    since_signal = [peak, tail]
    # одиночные проверки (candles[-1], поглощение) не должны сработать раньше комбо
    candles = [{"open": 90, "high": 91, "low": 89, "close": 90.5}, tail]
    pattern = find_reversal_pattern(candles, since_signal, bearish=True)
    assert pattern is not None
    assert pattern["kind"] == "пин-бар за 10 мин"
    assert pattern["entry_open"] == 111   # open ХВОСТА, а не 100 (open пика)
    assert pattern["candle"]["open"] == 100   # combo["open"] по-прежнему от пика — для стопа/лога это ок
