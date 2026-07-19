import sqlite3
from datetime import datetime, timezone

from crypto_bot import init_signals_db, save_signal, save_signal_check


def test_init_creates_tables(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signals", "signal_checks"} <= tables


def test_save_signal_returns_id_and_persists_row(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    signal_id = save_signal(
        "BTCUSDT", "BTC/USDT", "long", 100.0,
        datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc), "🟢 тренд вверх",
        db_path=db_path
    )
    assert signal_id == 1
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT symbol, direction, entry_price, trend_label FROM signals WHERE id = ?", (signal_id,)).fetchone()
    assert row == ("BTCUSDT", "long", 100.0, "🟢 тренд вверх")


def test_save_signal_check_persists_row(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    signal_id = save_signal(
        "BTCUSDT", "BTC/USDT", "long", 100.0,
        datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc), None,
        db_path=db_path
    )
    save_signal_check(signal_id, 15, 2.5, "🟢 в плюс", db_path=db_path)
    save_signal_check(signal_id, 60, None, None, db_path=db_path)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT minutes, pct, verdict FROM signal_checks WHERE signal_id = ? ORDER BY minutes", (signal_id,)
    ).fetchall()
    assert rows == [(15, 2.5, "🟢 в плюс"), (60, None, None)]
