import sqlite3
from datetime import datetime, timezone

from crypto_bot import (
    init_signals_db, save_signal, save_signal_check,
    save_pullback_tracking, has_active_pullback_tracking,
    get_active_pullback_tracking, update_pullback_tracking,
)


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


def test_init_creates_pullback_tracking_table(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "pullback_tracking" in tables


def test_save_pullback_tracking_returns_id_and_persists_row(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    now = datetime(2026, 7, 21, 0, 5, tzinfo=timezone.utc)

    tracking_id = save_pullback_tracking(
        "LAUSDT", "LAU/USDT", "long", 7, 0.050, 0.045, 0.0515, now, db_path=db_path
    )

    assert tracking_id == 1
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT symbol, direction, entry_period, status FROM pullback_tracking WHERE id = ?", (tracking_id,)
    ).fetchone()
    assert row == ("LAUSDT", "long", 7, "active")


def test_has_active_pullback_tracking_true_after_save(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    now = datetime(2026, 7, 21, 0, 5, tzinfo=timezone.utc)
    save_pullback_tracking("LAUSDT", "LAU/USDT", "long", 7, 0.050, 0.045, 0.0515, now, db_path=db_path)

    assert has_active_pullback_tracking("LAUSDT", "long", db_path=db_path) is True
    assert has_active_pullback_tracking("LAUSDT", "short", db_path=db_path) is False
    assert has_active_pullback_tracking("OTHERUSDT", "long", db_path=db_path) is False


def test_get_active_pullback_tracking_returns_only_active(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    now = datetime(2026, 7, 21, 0, 5, tzinfo=timezone.utc)
    id1 = save_pullback_tracking("LAUSDT", "LAU/USDT", "long", 7, 0.050, 0.045, 0.0515, now, db_path=db_path)
    save_pullback_tracking("BTCUSDT", "BTC/USDT", "short", 14, 100.0, 105.0, 97.0, now, db_path=db_path)
    update_pullback_tracking(id1, 7, 0.050, 0.045, 0.0515, "done", now, db_path=db_path)

    rows = get_active_pullback_tracking(db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["entry_period"] == 14


def test_update_pullback_tracking_advances_entry_period(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signals_db(db_path)
    now = datetime(2026, 7, 21, 0, 5, tzinfo=timezone.utc)
    tracking_id = save_pullback_tracking("LAUSDT", "LAU/USDT", "long", 7, 0.050, 0.045, 0.0515, now, db_path=db_path)

    later = datetime(2026, 7, 22, 0, 5, tzinfo=timezone.utc)
    update_pullback_tracking(tracking_id, 14, 0.060, 0.055, 0.0618, "active", later, db_path=db_path)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT entry_period, entry, stop, take, status FROM pullback_tracking WHERE id = ?", (tracking_id,)
    ).fetchone()
    assert row == (14, 0.060, 0.055, 0.0618, "active")
