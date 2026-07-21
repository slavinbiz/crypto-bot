# Довыставление контр-сигнала по мере пробоя недельных EMA — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После начального контр-сигнала (лимитка на ближайшей недельной EMA) бот продолжает следить за парой и, когда дневное закрытие проходит текущий вход, довыставляет сигнал на следующей EMA (7→14→28), пока EMA не кончатся или сетап не сломается пробоем стопа.

**Architecture:** Состояние трекинга персистится в новой таблице `pullback_tracking` в существующей `signals.db` (не в памяти — бот передеплоится почти каждую сессию). Раз в сутки, после закрытия дневной свечи UTC, встроенная в уже существующий `refresh_loop()` проверка сканирует активные записи и для каждой решает: продвинуть, отменить или завершить трекинг. Вся расчётная логика (выбор следующей EMA, решение по дневному закрытию) — чистые функции в `ema_pullback.py`, без сети и без БД; сеть/БД/Telegram — в `crypto_bot.py`, как и весь остальной код.

**Tech Stack:** Python 3.11, numpy, sqlite3, python-telegram-bot, pytest.

## Global Constraints

- Тесты запускаются `py -3.11 -m pytest tests/ -q` (глобальный `python`/`py -3.14` не видит numpy — используем `py -3.11`).
- База данных сигналов: `/root/signals.db` на сервере, путь передаётся параметром `db_path` во все функции (как у существующих `save_signal`/`save_signal_check`) — тесты используют `tmp_path`.
- Периоды EMA и их порядок продвижения — `ema_pullback.EMA_PERIODS = [7, 14, 28]`, менять не нужно.
- Не менять поведение существующего начального контр-сигнала (`build_pullback_signal`, `fetch_pullback_signal`, `fmt_pullback_caption`) — только добавлять трекинг поверх.
- Формат коммитов в этом репозитории — обычный Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`), без префикса `[agent]` (это `crypto_bot` репозиторий, не `jarvis`).

---

### Task 1: `build_pullback_signal_for_period` — вход на конкретной EMA вместо ближайшей

**Files:**
- Modify: `ema_pullback.py:55-98`
- Test: `tests/test_ema_pullback.py`

**Interfaces:**
- Produces: `build_pullback_signal_for_period(counter_direction: str, weekly_candles: list[dict], entry_period: int) -> dict | None`. Возвращает `{"direction", "entry_period", "entry", "stop", "take", "emas"}` или `None`, если `entry_period` не прогрелся (недостаточно недельных свечей) или для него нет настоящего недельного уровня под стоп дальше входа, либо дистанция стоп-вход превышает санитарный потолок.
- `build_pullback_signal(direction, price, weekly_candles)` сохраняет старую сигнатуру и поведение — теперь просто находит ближайший период на контр-стороне и делегирует в `build_pullback_signal_for_period`.

- [ ] **Step 1: Написать падающие тесты на новую функцию**

Добавить в конец `tests/test_ema_pullback.py`:

```python
def test_build_pullback_signal_for_period_matches_wrapper_result():
    # Тот же кейс, что и test_build_pullback_signal_long_counter_to_dump, но период задан явно
    closes = list(np.linspace(2.0, 1.0, 100))
    candles = make_weekly_candles(closes, {
        0: {"low": 1.02},
        1: {"low": 1.05},
        2: {"low": 1.03},
    })

    result = build_pullback_signal_for_period("long", weekly_candles=candles, entry_period=14)

    assert result is not None
    assert result["direction"] == "long"
    assert result["entry_period"] == 14
    assert result["stop"] == pytest.approx(1.05 * 0.98)
    assert result["take"] == pytest.approx(result["entry"] * 1.03)


def test_build_pullback_signal_for_period_none_when_period_not_warmed_up():
    # Всего 30 свечей — хватает только на EMA7 (30 >= 7*3), EMA14 не прогрета (30 < 14*3)
    closes = [c["close"] for c in make_candles(30)]
    candles = make_weekly_candles(closes)

    result = build_pullback_signal_for_period("long", weekly_candles=candles, entry_period=14)

    assert result is None


def test_build_pullback_signal_for_period_none_when_no_real_level_beyond_entry():
    closes = list(np.linspace(2.0, 1.0, 100))
    candles = make_weekly_candles(closes)  # без overrides — low всегда 1e9, стоп не за что поставить

    result = build_pullback_signal_for_period("long", weekly_candles=candles, entry_period=14)

    assert result is None
```

Обновить импорт в начале файла:

```python
from ema_pullback import (
    calc_weekly_emas, build_pullback_signal, build_pullback_signal_for_period,
    EMA_PERIODS, EMA_WARMUP_FACTOR,
)
```

- [ ] **Step 2: Запустить тесты, убедиться что новые падают**

Run: `py -3.11 -m pytest tests/test_ema_pullback.py -q`
Expected: 3 ошибки `ImportError: cannot import name 'build_pullback_signal_for_period'` (остальные 8 существующих тестов не запустятся из-за той же ошибки импорта — это нормально, весь файл падает на импорте).

- [ ] **Step 3: Реализовать `build_pullback_signal_for_period`, отрефакторить `build_pullback_signal`**

Заменить в `ema_pullback.py` блок с строки 55 (`def build_pullback_signal...`) по строку 98 (конец функции) на:

```python
def build_pullback_signal_for_period(counter_direction: str, weekly_candles: list[dict], entry_period: int) -> dict | None:
    """Вход на КОНКРЕТНОЙ недельной EMA (а не на ближайшей к цене) — используется как
    начальным контр-сигналом (через build_pullback_signal), так и трекингом при
    довыставлении на следующую EMA после пробоя текущей.
    counter_direction — направление самого контр-сигнала ("long"/"short"), не исходного памп/дамп.
    None — если период не прогрелся или для него нет структуры под стоп."""
    closes = np.array([c["close"] for c in weekly_candles])
    emas = calc_weekly_emas(closes)

    if entry_period not in emas:
        return None
    entry = emas[entry_period]

    stop_level = nearest_weekly_stop_level(weekly_candles, entry, counter_direction)
    if stop_level is None:
        return None

    if counter_direction == "long":
        stop = stop_level * (1 - STOP_BUFFER_PCT / 100)
        take = entry * (1 + TAKE_PROFIT_PCT / 100)
    else:
        stop = stop_level * (1 + STOP_BUFFER_PCT / 100)
        take = entry * (1 - TAKE_PROFIT_PCT / 100)

    stop_distance_pct = abs(entry - stop) / entry * 100
    if stop_distance_pct > TAKE_PROFIT_PCT * STOP_MAX_DISTANCE_FACTOR:
        return None

    return {
        "direction": counter_direction,
        "entry_period": entry_period,
        "entry": entry,
        "stop": stop,
        "take": take,
        "emas": emas,
    }


def build_pullback_signal(direction: str, price: float, weekly_candles: list[dict]) -> dict | None:
    """direction — направление ИСХОДНОГО памп/дамп сигнала ("long" на пампе, "short" на дампе).
    Контр-сигнал открывается в противоположную сторону, на ближайшей к цене недельной EMA.
    None — если недельной структуры (EMA для входа или реального хая/лоя дальше входа для
    стопа) не хватает."""
    closes = np.array([c["close"] for c in weekly_candles])
    emas = calc_weekly_emas(closes)

    counter_direction = "short" if direction == "long" else "long"

    if counter_direction == "long":
        side = {p: v for p, v in emas.items() if v < price}
    else:
        side = {p: v for p, v in emas.items() if v > price}

    if not side:
        return None

    pick_entry = max if counter_direction == "long" else min
    entry_period = pick_entry(side, key=lambda p: side[p])

    return build_pullback_signal_for_period(counter_direction, weekly_candles, entry_period)
```

- [ ] **Step 4: Запустить все тесты `test_ema_pullback.py`, убедиться что все проходят**

Run: `py -3.11 -m pytest tests/test_ema_pullback.py -v`
Expected: 11 passed (8 существующих + 3 новых), 0 failed.

- [ ] **Step 5: Коммит**

```bash
git add ema_pullback.py tests/test_ema_pullback.py
git commit -m "refactor: вынести build_pullback_signal_for_period — вход на конкретной EMA"
```

---

### Task 2: `next_pullback_period` — следующая EMA в цепочке 7→14→28

**Files:**
- Modify: `ema_pullback.py` (добавить функцию в конец файла)
- Test: `tests/test_ema_pullback.py`

**Interfaces:**
- Consumes: `EMA_PERIODS = [7, 14, 28]` (уже объявлен в файле).
- Produces: `next_pullback_period(current_period: int) -> int | None`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_ema_pullback.py`:

```python
def test_next_pullback_period_returns_next_in_chain():
    assert next_pullback_period(7) == 14
    assert next_pullback_period(14) == 28


def test_next_pullback_period_none_after_last():
    assert next_pullback_period(28) is None
```

Обновить импорт:

```python
from ema_pullback import (
    calc_weekly_emas, build_pullback_signal, build_pullback_signal_for_period,
    next_pullback_period, EMA_PERIODS, EMA_WARMUP_FACTOR,
)
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `py -3.11 -m pytest tests/test_ema_pullback.py -q`
Expected: `ImportError: cannot import name 'next_pullback_period'`

- [ ] **Step 3: Реализовать**

Добавить в конец `ema_pullback.py`:

```python
def next_pullback_period(current_period: int) -> int | None:
    """Следующий период в цепочке EMA_PERIODS после текущего. None — если текущий последний."""
    idx = EMA_PERIODS.index(current_period)
    return EMA_PERIODS[idx + 1] if idx + 1 < len(EMA_PERIODS) else None
```

- [ ] **Step 4: Запустить тесты**

Run: `py -3.11 -m pytest tests/test_ema_pullback.py -v`
Expected: 13 passed.

- [ ] **Step 5: Коммит**

```bash
git add ema_pullback.py tests/test_ema_pullback.py
git commit -m "feat: next_pullback_period — следующая EMA в цепочке 7→14→28"
```

---

### Task 3: `evaluate_tracking` — решение по дневному закрытию

**Files:**
- Modify: `ema_pullback.py` (добавить функцию в конец файла)
- Test: `tests/test_ema_pullback.py`

**Interfaces:**
- Produces: `evaluate_tracking(direction: str, entry: float, stop: float, daily_close: float) -> str`, возвращает `"advance"` / `"invalidate"` / `"none"`. `direction` — направление контр-сигнала ("long"/"short"), для long структурно `stop < entry`, для short `stop > entry`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_ema_pullback.py`:

```python
def test_evaluate_tracking_long_advance_when_close_above_entry():
    assert evaluate_tracking("long", entry=100.0, stop=95.0, daily_close=101.0) == "advance"


def test_evaluate_tracking_long_invalidate_when_close_below_stop():
    assert evaluate_tracking("long", entry=100.0, stop=95.0, daily_close=94.0) == "invalidate"


def test_evaluate_tracking_long_none_between_stop_and_entry():
    assert evaluate_tracking("long", entry=100.0, stop=95.0, daily_close=97.0) == "none"


def test_evaluate_tracking_long_boundary_at_entry_is_none():
    assert evaluate_tracking("long", entry=100.0, stop=95.0, daily_close=100.0) == "none"


def test_evaluate_tracking_long_boundary_at_stop_is_none():
    assert evaluate_tracking("long", entry=100.0, stop=95.0, daily_close=95.0) == "none"


def test_evaluate_tracking_short_advance_when_close_below_entry():
    assert evaluate_tracking("short", entry=100.0, stop=105.0, daily_close=99.0) == "advance"


def test_evaluate_tracking_short_invalidate_when_close_above_stop():
    assert evaluate_tracking("short", entry=100.0, stop=105.0, daily_close=106.0) == "invalidate"


def test_evaluate_tracking_short_none_between_entry_and_stop():
    assert evaluate_tracking("short", entry=100.0, stop=105.0, daily_close=103.0) == "none"
```

Обновить импорт:

```python
from ema_pullback import (
    calc_weekly_emas, build_pullback_signal, build_pullback_signal_for_period,
    next_pullback_period, evaluate_tracking, EMA_PERIODS, EMA_WARMUP_FACTOR,
)
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `py -3.11 -m pytest tests/test_ema_pullback.py -q`
Expected: `ImportError: cannot import name 'evaluate_tracking'`

- [ ] **Step 3: Реализовать**

Добавить в конец `ema_pullback.py`:

```python
def evaluate_tracking(direction: str, entry: float, stop: float, daily_close: float) -> str:
    """Что делать с активным трекингом контр-сигнала по дневному закрытию:
    "advance" — закрытие прошло дальше входа (пора ждать отскока от следующей EMA),
    "invalidate" — закрытие пробило стоп (структура сломана, трекинг отменяем),
    "none" — ничего не изменилось, продолжаем ждать."""
    if direction == "long":
        if daily_close < stop:
            return "invalidate"
        if daily_close > entry:
            return "advance"
    else:
        if daily_close > stop:
            return "invalidate"
        if daily_close < entry:
            return "advance"
    return "none"
```

- [ ] **Step 4: Запустить тесты**

Run: `py -3.11 -m pytest tests/test_ema_pullback.py -v`
Expected: 21 passed.

- [ ] **Step 5: Коммит**

```bash
git add ema_pullback.py tests/test_ema_pullback.py
git commit -m "feat: evaluate_tracking — решение advance/invalidate/none по дневному закрытию"
```

---

### Task 4: Таблица `pullback_tracking` и функции доступа к БД

**Files:**
- Modify: `crypto_bot.py:154-176` (`init_signals_db`), добавить функции после `save_signal_check` (после текущей строки 205)
- Test: `tests/test_signals_db.py`

**Interfaces:**
- Produces:
  - `save_pullback_tracking(symbol: str, pair_name: str, direction: str, entry_period: int, entry: float, stop: float, take: float, now: datetime, db_path: str = SIGNALS_DB) -> int`
  - `has_active_pullback_tracking(symbol: str, direction: str, db_path: str = SIGNALS_DB) -> bool`
  - `get_active_pullback_tracking(db_path: str = SIGNALS_DB) -> list[dict]` — каждый dict содержит `id, symbol, pair_name, direction, entry_period, entry, stop, take`.
  - `update_pullback_tracking(tracking_id: int, entry_period: int, entry: float, stop: float, take: float, status: str, now: datetime, db_path: str = SIGNALS_DB) -> None`

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/test_signals_db.py`:

```python
from datetime import datetime, timezone
from crypto_bot import (
    init_signals_db, save_signal, save_signal_check,
    save_pullback_tracking, has_active_pullback_tracking,
    get_active_pullback_tracking, update_pullback_tracking,
)


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
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `py -3.11 -m pytest tests/test_signals_db.py -q`
Expected: `ImportError: cannot import name 'save_pullback_tracking'`

- [ ] **Step 3: Реализовать**

В `crypto_bot.py` заменить блок `init_signals_db` (строки 154-176) — добавить создание третьей таблицы перед `conn.commit()`:

```python
def init_signals_db(db_path: str = SIGNALS_DB) -> None:
    """Создать таблицы signals/signal_checks/pullback_tracking, если их ещё нет."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            pair_name TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            signal_time TEXT NOT NULL,
            trend_label TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_checks (
            signal_id INTEGER NOT NULL REFERENCES signals(id),
            minutes INTEGER NOT NULL,
            pct REAL,
            verdict TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pullback_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            pair_name TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_period INTEGER NOT NULL,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            take REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
```

Добавить в `crypto_bot.py` сразу после `save_signal_check` (после текущей строки 205, перед комментарием `# ─── ЛОГИРОВАНИЕ`):

```python
def save_pullback_tracking(symbol: str, pair_name: str, direction: str, entry_period: int,
                            entry: float, stop: float, take: float, now: datetime,
                            db_path: str = SIGNALS_DB) -> int:
    """Записать новый активный трекинг контр-сигнала, вернуть его id."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO pullback_tracking "
        "(symbol, pair_name, direction, entry_period, entry, stop, take, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (symbol, pair_name, direction, entry_period, entry, stop, take, now.isoformat(), now.isoformat())
    )
    conn.commit()
    tracking_id = cur.lastrowid
    conn.close()
    return tracking_id


def has_active_pullback_tracking(symbol: str, direction: str, db_path: str = SIGNALS_DB) -> bool:
    """True если для пары+направления уже есть активный трекинг (не плодим дубли)."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM pullback_tracking WHERE symbol = ? AND direction = ? AND status = 'active' LIMIT 1",
        (symbol, direction)
    ).fetchone()
    conn.close()
    return row is not None


def get_active_pullback_tracking(db_path: str = SIGNALS_DB) -> list[dict]:
    """Все активные записи трекинга контр-сигналов."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol, pair_name, direction, entry_period, entry, stop, take "
        "FROM pullback_tracking WHERE status = 'active'"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_pullback_tracking(tracking_id: int, entry_period: int, entry: float, stop: float,
                              take: float, status: str, now: datetime,
                              db_path: str = SIGNALS_DB) -> None:
    """Обновить запись трекинга — продвижение на следующую EMA, отмена или завершение."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE pullback_tracking SET entry_period = ?, entry = ?, stop = ?, take = ?, status = ?, updated_at = ? "
        "WHERE id = ?",
        (entry_period, entry, stop, take, status, now.isoformat(), tracking_id)
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 4: Запустить тесты**

Run: `py -3.11 -m pytest tests/test_signals_db.py -v`
Expected: все тесты (старые + 5 новых) passed.

- [ ] **Step 5: Коммит**

```bash
git add crypto_bot.py tests/test_signals_db.py
git commit -m "feat: таблица pullback_tracking и функции доступа для трекинга контр-сигнала"
```

---

### Task 5: `fetch_tracking_update` — сеть + чистая логика для одной строки трекинга

**Files:**
- Modify: `crypto_bot.py` (добавить функцию сразу после `fetch_pullback_signal`, текущие строки 293-299)
- Test: `tests/test_fetch_tracking_update.py` (новый файл)

**Interfaces:**
- Consumes: `get_klines(symbol, interval, limit, timeout=10)` (модульная функция, уже есть); `ema_pullback.evaluate_tracking`, `ema_pullback.next_pullback_period`, `ema_pullback.build_pullback_signal_for_period`, `ema_pullback.WEEKLY_INTERVAL`, `ema_pullback.WEEKLY_LIMIT` (Task 1-3).
- Produces: `fetch_tracking_update(symbol: str, direction: str, entry_period: int, entry: float, stop: float) -> dict | None`.
  Возвращает:
  - `None` — сетевая ошибка или дневных свечей меньше 2 (нет закрытой свечи).
  - `{"decision": "none", "daily_close": float}`
  - `{"decision": "invalidate", "daily_close": float}`
  - `{"decision": "done"}` — дошли до последней EMA или следующая EMA не даёт структуры для стопа.
  - `{"decision": "advance", "pullback": dict}` — `pullback` в том же формате, что возвращает `build_pullback_signal_for_period`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_fetch_tracking_update.py`:

```python
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
            return [{"close": 0.0}, {"close": daily_close}]
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
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `py -3.11 -m pytest tests/test_fetch_tracking_update.py -q`
Expected: `AttributeError: module 'crypto_bot' has no attribute 'fetch_tracking_update'`

- [ ] **Step 3: Реализовать**

Добавить в `crypto_bot.py` сразу после `fetch_pullback_signal` (после текущей строки 299, перед `def fmt_pullback_caption`):

```python
def fetch_tracking_update(symbol: str, direction: str, entry_period: int, entry: float, stop: float) -> dict | None:
    """Дневное закрытие + (если нужно) недельные свечи → решение по трекингу контр-сигнала.
    direction — направление контр-сигнала ("long"/"short"). None — сетевая ошибка, трекинг
    в этом цикле не трогаем."""
    try:
        daily_candles = get_klines(symbol, "1d", 2, timeout=4)
    except Exception as e:
        log.warning(f"Не удалось проверить трекинг {symbol}: {e}")
        return None
    if len(daily_candles) < 2:
        return None

    daily_close = daily_candles[-2]["close"]
    decision = ema_pullback.evaluate_tracking(direction, entry, stop, daily_close)

    if decision != "advance":
        return {"decision": decision, "daily_close": daily_close}

    next_period = ema_pullback.next_pullback_period(entry_period)
    if next_period is None:
        return {"decision": "done"}

    try:
        weekly_candles = get_klines(symbol, ema_pullback.WEEKLY_INTERVAL, ema_pullback.WEEKLY_LIMIT, timeout=4)
    except Exception as e:
        log.warning(f"Не удалось проверить трекинг {symbol}: {e}")
        return None

    pullback = ema_pullback.build_pullback_signal_for_period(direction, weekly_candles, next_period)
    if pullback is None:
        return {"decision": "done"}
    return {"decision": "advance", "pullback": pullback}
```

- [ ] **Step 4: Запустить тесты**

Run: `py -3.11 -m pytest tests/test_fetch_tracking_update.py -v`
Expected: 7 passed.

- [ ] **Step 5: Коммит**

```bash
git add crypto_bot.py tests/test_fetch_tracking_update.py
git commit -m "feat: fetch_tracking_update — решение по трекингу контр-сигнала на дневном закрытии"
```

---

### Task 6: Создание трекинга при начальном контр-сигнале

**Files:**
- Modify: `crypto_bot.py:875-887` (`send_pullback_signal`)

**Interfaces:**
- Consumes: `has_active_pullback_tracking`, `save_pullback_tracking` (Task 4).

Прямого юнит-теста нет — `send_pullback_signal` вложена в `signal_loop` и требует живой `bot`/Telegram, как и соседние `verify_signal`/`send_pullback_signal` (существующий код тоже не тестирует эту функцию напрямую — тестируются только чистые/сетевые обёртки типа `fetch_pullback_signal`). Проверяется вручную на Step 3.

- [ ] **Step 1: Изменить `send_pullback_signal`**

Заменить текущий блок (строки 875-887):

```python
    async def send_pullback_signal(symbol: str, direction: str, price: float, pair_name: str):
        """Контр-сигнал по недельным EMA против направления памп/дамп. Тихо ничего не шлёт, если структуры не хватает."""
        pullback = await asyncio.to_thread(fetch_pullback_signal, symbol, direction, price)
        if pullback is None:
            return
        log.info(
            f"Контр-сигнал: {symbol} — {pullback['direction'].upper()} "
            f"вход={pullback['entry']:.5g} стоп={pullback['stop']:.5g} тейк={pullback['take']:.5g}"
        )
        try:
            await bot.send_message(CHAT_ID, fmt_pullback_caption(pair_name, pullback), parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Ошибка отправки контр-сигнала {symbol}: {e}")
```

на:

```python
    async def send_pullback_signal(symbol: str, direction: str, price: float, pair_name: str):
        """Контр-сигнал по недельным EMA против направления памп/дамп. Тихо ничего не шлёт, если структуры
        не хватает или для этой пары+направления уже есть активный трекинг."""
        pullback = await asyncio.to_thread(fetch_pullback_signal, symbol, direction, price)
        if pullback is None:
            return
        already_tracking = await asyncio.to_thread(has_active_pullback_tracking, symbol, pullback["direction"])
        if already_tracking:
            return
        log.info(
            f"Контр-сигнал: {symbol} — {pullback['direction'].upper()} "
            f"вход={pullback['entry']:.5g} стоп={pullback['stop']:.5g} тейк={pullback['take']:.5g}"
        )
        try:
            await bot.send_message(CHAT_ID, fmt_pullback_caption(pair_name, pullback), parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Ошибка отправки контр-сигнала {symbol}: {e}")
            return
        await asyncio.to_thread(
            save_pullback_tracking, symbol, pair_name, pullback["direction"], pullback["entry_period"],
            pullback["entry"], pullback["stop"], pullback["take"], datetime.now(timezone.utc)
        )
```

- [ ] **Step 2: Прогнать весь набор тестов — регрессии быть не должно**

Run: `py -3.11 -m pytest tests/ -q`
Expected: все тесты passed (это изменение не тестируется напрямую, но не должно ничего сломать).

- [ ] **Step 3: Ручная проверка синтаксиса**

Run: `py -3.11 -c "import ast; ast.parse(open('crypto_bot.py', encoding='utf-8').read())"`
Expected: без вывода (файл синтаксически корректен) — живой прогон бота с реальным Telegram-токеном делаем на деплое (Task 8).

- [ ] **Step 4: Коммит**

```bash
git add crypto_bot.py
git commit -m "feat: создавать pullback_tracking при отправке начального контр-сигнала"
```

---

### Task 7: Суточная проверка трекинга — `check_pullback_tracking` + вызов из `refresh_loop`

**Files:**
- Modify: `crypto_bot.py:888` (добавить функцию перед `verify_signal`)
- Modify: `crypto_bot.py:949-1011` (`refresh_loop`)

**Interfaces:**
- Consumes: `get_active_pullback_tracking`, `update_pullback_tracking` (Task 4), `fetch_tracking_update` (Task 5), `fmt_pullback_caption` (существующая).

Как и `send_pullback_signal`, эта функция — orchestration-код внутри `signal_loop`, без прямого юнит-теста (вся принимающая решения логика уже покрыта тестами `fetch_tracking_update`/`evaluate_tracking` из Task 3 и 5). Проверяется вручную на Step 3-4.

- [ ] **Step 1: Добавить `check_pullback_tracking`**

Вставить в `crypto_bot.py` сразу после `send_pullback_signal` (после текущей строки 887, перед `async def verify_signal`):

```python
    async def check_pullback_tracking():
        """Раз в сутки — проверяем все активные трекинги контр-сигналов на продвижение/отмену/завершение."""
        rows = await asyncio.to_thread(get_active_pullback_tracking)
        for row in rows:
            result = await asyncio.to_thread(
                fetch_tracking_update, row["symbol"], row["direction"], row["entry_period"], row["entry"], row["stop"]
            )
            if result is None or result["decision"] == "none":
                continue

            now_dt = datetime.now(timezone.utc)

            if result["decision"] == "invalidate":
                await asyncio.to_thread(
                    update_pullback_tracking, row["id"], row["entry_period"], row["entry"], row["stop"],
                    row["take"], "invalidated", now_dt
                )
                try:
                    await bot.send_message(
                        CHAT_ID,
                        f"🔻 Сетап <code>{row['pair_name']}</code> сломан — дневное закрытие "
                        f"{result['daily_close']:.5g} пробило стоп {row['stop']:.5g}",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    log.warning(f"Ошибка отправки отмены трекинга {row['symbol']}: {e}")
                continue

            if result["decision"] == "done":
                await asyncio.to_thread(
                    update_pullback_tracking, row["id"], row["entry_period"], row["entry"], row["stop"],
                    row["take"], "done", now_dt
                )
                try:
                    await bot.send_message(
                        CHAT_ID,
                        f"🏁 <code>{row['pair_name']}</code> прошёл все EMA, дальше пробивать нечего",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    log.warning(f"Ошибка отправки завершения трекинга {row['symbol']}: {e}")
                continue

            # decision == "advance"
            pullback = result["pullback"]
            await asyncio.to_thread(
                update_pullback_tracking, row["id"], pullback["entry_period"], pullback["entry"], pullback["stop"],
                pullback["take"], "active", now_dt
            )
            try:
                await bot.send_message(CHAT_ID, fmt_pullback_caption(row["pair_name"], pullback), parse_mode=ParseMode.HTML)
            except Exception as e:
                log.warning(f"Ошибка отправки продвижения трекинга {row['symbol']}: {e}")
```

- [ ] **Step 2: Встроить суточный вызов в `refresh_loop`**

Изменить начало `refresh_loop` (текущие строки 949-953):

```python
    async def refresh_loop():
        """Обновляем тикеры, фандинг и список пар. Реагируем на смену настроек."""
        nonlocal valid_symbols, tickers_24h
        last_funding = time.time()
        last_full    = time.time()
```

на:

```python
    async def refresh_loop():
        """Обновляем тикеры, фандинг и список пар. Реагируем на смену настроек."""
        nonlocal valid_symbols, tickers_24h
        last_funding = time.time()
        last_full    = time.time()
        last_pullback_date = None
```

Добавить в конец тела `while True` (после текущего блока `# Каждые 12 часов — полный сброс`, после строки `last_full = now` / `except Exception as e: log.warning(...)` — то есть сразу после текущей строки 1010, перед пустой строкой и комментарием `# Запускаем websocket и refresh параллельно`):

```python

            # Раз в сутки, после закрытия дневной свечи (00:05 UTC) — проверяем трекинг контр-сигналов
            now_dt = datetime.now(timezone.utc)
            if (now_dt.hour, now_dt.minute) >= (0, 5) and last_pullback_date != now_dt.date():
                try:
                    await check_pullback_tracking()
                    last_pullback_date = now_dt.date()
                except Exception as e:
                    log.warning(f"Ошибка проверки трекинга контр-сигналов: {e}")
```

- [ ] **Step 3: Прогнать весь набор тестов**

Run: `py -3.11 -m pytest tests/ -q`
Expected: все тесты passed.

- [ ] **Step 4: Ручная проверка синтаксиса и локального импорта**

Run: `py -3.11 -c "import ast; ast.parse(open('crypto_bot.py', encoding='utf-8').read())"`
Expected: без вывода.

- [ ] **Step 5: Коммит**

```bash
git add crypto_bot.py
git commit -m "feat: суточная проверка трекинга контр-сигнала в refresh_loop"
```

---

### Task 8: Деплой на TimeWeb

**Files:** нет (только сервер)

- [ ] **Step 1: Скопировать изменённые файлы на сервер**

Run: `scp crypto_bot.py ema_pullback.py root@64.188.57.249:/root/`

- [ ] **Step 2: Перезапустить сервис**

Run: `ssh root@64.188.57.249 "systemctl restart crypto-bot"`

- [ ] **Step 3: Проверить чистый старт по логам**

Run: `ssh root@64.188.57.249 "journalctl -u crypto-bot -n 40 --no-pager"`
Expected: нет traceback/ошибок импорта, видно сообщение о старте (`🤖 Бот запущен`) в логе или в Telegram.

- [ ] **Step 4: Обновить дневник**

Добавить запись в `memory/2026-07-21.md` (репозиторий `jarvis`) в раздел «Сделано» — что задеплоено, и в TODO — «понаблюдать за первым продвижением трекинга на реальной паре (LAUSDT) через сутки после закрытия дневной свечи».
