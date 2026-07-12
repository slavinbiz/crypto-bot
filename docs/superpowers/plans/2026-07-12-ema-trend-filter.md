# EMA Trend Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в сигналы памп/дамп вердикт «стоит ли входить» на основе тренда (1H EMA50/EMA200) и близости цены к EMA20 на 15m.

**Architecture:** Новый модуль `ema_trend.py` с чистыми функциями расчёта EMA и вердикта (без сетевых вызовов, легко тестируется). `crypto_bot.py` получает тонкую обёртку `fetch_trend_verdict()`, которая дёргает уже существующий `get_klines()` и подставляет вердикт в `fmt_caption()`.

**Tech Stack:** Python 3.10+, numpy, pytest (тесты запускаются локально: `cd C:\crypto_bot && python -m pytest tests/ -v`).

## Global Constraints

- Спека: `docs/superpowers/specs/2026-07-12-ema-trend-filter-design.md`
- 1H тренд: EMA50 vs EMA200. 15m положение: EMA20, порог 0.5% (настраиваемый через `bot_settings.json`, ключ `EMA_DISTANCE_THRESHOLD_PCT`)
- Алерт отправляется всегда (вердикт не блокирует отправку)
- REST-запросы klines для тренда делаются только при сработавшем сигнале (внутри `process_kline`, не на каждый тик вебсокета)
- Ошибка сети → вердикт `⚪ Не удалось проверить`, сигнал уходит без задержки
- Файл `crypto_bot.py` не переписывается целиком — только точечные правки (см. `CLAUDE.md`: "Если файл > 100 строк — правь точечно")
- Коммит после каждой задачи

---

### Task 1: `calc_ema` — расчёт EMA

**Files:**
- Create: `C:\crypto_bot\ema_trend.py`
- Test: `C:\crypto_bot\tests\test_ema_trend.py`

**Interfaces:**
- Produces: `calc_ema(closes: np.ndarray, period: int) -> float` — EMA seed на SMA первых `period` значений, затем рекурсивно по оставшимся. Бросает `ValueError` если `len(closes) < period`.

- [ ] **Step 1: Создать `tests/__init__.py` (пустой) и написать падающий тест**

```python
# tests/test_ema_trend.py
import numpy as np
import pytest
from ema_trend import calc_ema


def test_calc_ema_basic():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    # seed = mean(10,11,12) = 11.0; alpha = 2/(3+1) = 0.5
    # шаг1: 0.5*13 + 0.5*11.0 = 12.0
    # шаг2: 0.5*14 + 0.5*12.0 = 13.0
    result = calc_ema(closes, period=3)
    assert result == pytest.approx(13.0)


def test_calc_ema_insufficient_data_raises():
    closes = np.array([10.0, 11.0])
    with pytest.raises(ValueError):
        calc_ema(closes, period=3)
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_trend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ema_trend'`

- [ ] **Step 3: Создать `ema_trend.py` с минимальной реализацией**

```python
"""Тренд-фильтр по EMA: вердикт для сигналов памп/дамп."""
import numpy as np

EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200
EMA_PULLBACK = 20
EMA_DISTANCE_THRESHOLD_PCT = 0.5

TREND_INTERVAL = "1h"
TREND_LIMIT = 210
PULLBACK_INTERVAL = "15m"
PULLBACK_LIMIT = 30


def calc_ema(closes: np.ndarray, period: int) -> float:
    """EMA: seed — SMA первых `period` значений, затем рекурсивно."""
    if len(closes) < period:
        raise ValueError(f"нужно минимум {period} свечей, получено {len(closes)}")
    alpha = 2 / (period + 1)
    ema = closes[:period].mean()
    for price in closes[period:]:
        ema = alpha * price + (1 - alpha) * ema
    return float(ema)
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_trend.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd C:\crypto_bot
git add ema_trend.py tests/
git commit -m "feat: calc_ema — расчёт EMA для тренд-фильтра"
```

---

### Task 2: `get_trend_verdict` — вердикт по тренду и положению к EMA

**Files:**
- Modify: `C:\crypto_bot\ema_trend.py`
- Test: `C:\crypto_bot\tests\test_ema_trend.py`

**Interfaces:**
- Consumes: `calc_ema(closes: np.ndarray, period: int) -> float` (Task 1)
- Produces: `get_trend_verdict(direction: str, price: float, trend_candles: list[dict], pullback_candles: list[dict]) -> dict` — возвращает `{"verdict": str, "label": str, "distance_pct": float}`. `verdict` один из `"trend_at_ema"`, `"trend_far_ema"`, `"against_trend"`. `trend_candles`/`pullback_candles` — списки словарей с ключом `"close"` (формат из существующего `get_klines()` в `crypto_bot.py`). `direction` — `"long"` или `"short"`.

- [ ] **Step 1: Написать падающие тесты**

```python
# добавить в tests/test_ema_trend.py
from ema_trend import get_trend_verdict


def _uptrend_candles(n=210, start=100.0, step=0.5):
    return [{"close": start + i * step} for i in range(n)]


def _flat_candles(n=30, price=100.0):
    return [{"close": price} for _ in range(n)]


def test_verdict_trend_at_ema_for_long():
    trend = _uptrend_candles()
    pullback = _flat_candles(price=100.0)
    result = get_trend_verdict("long", price=100.0, trend_candles=trend, pullback_candles=pullback)
    assert result["verdict"] == "trend_at_ema"
    assert result["distance_pct"] == pytest.approx(0.0)


def test_verdict_trend_far_ema_for_long():
    trend = _uptrend_candles()
    pullback = _flat_candles(price=100.0)
    result = get_trend_verdict("long", price=110.0, trend_candles=trend, pullback_candles=pullback)
    assert result["verdict"] == "trend_far_ema"


def test_verdict_against_trend_for_short_in_uptrend():
    trend = _uptrend_candles()
    pullback = _flat_candles(price=100.0)
    result = get_trend_verdict("short", price=100.0, trend_candles=trend, pullback_candles=pullback)
    assert result["verdict"] == "against_trend"
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_trend.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_trend_verdict'`

- [ ] **Step 3: Добавить `get_trend_verdict` в `ema_trend.py`**

```python
# добавить в конец ema_trend.py
def get_trend_verdict(direction: str, price: float, trend_candles: list[dict], pullback_candles: list[dict]) -> dict:
    """Вердикт по 1H-тренду (EMA50/EMA200) и положению цены к EMA20 на 15m."""
    trend_closes = np.array([c["close"] for c in trend_candles])
    pullback_closes = np.array([c["close"] for c in pullback_candles])

    ema_fast = calc_ema(trend_closes, EMA_TREND_FAST)
    ema_slow = calc_ema(trend_closes, EMA_TREND_SLOW)
    trend_up = ema_fast > ema_slow

    ema_pullback = calc_ema(pullback_closes, EMA_PULLBACK)
    signed_distance_pct = (price - ema_pullback) / ema_pullback * 100
    distance_pct = abs(signed_distance_pct)

    trend_matches = (direction == "long" and trend_up) or (direction == "short" and not trend_up)

    if not trend_matches:
        return {
            "verdict": "against_trend",
            "label": "🔴 Против тренда",
            "distance_pct": distance_pct,
        }
    if distance_pct <= EMA_DISTANCE_THRESHOLD_PCT:
        return {
            "verdict": "trend_at_ema",
            "label": f"🟢 По тренду, у EMA20 ({signed_distance_pct:+.1f}%)",
            "distance_pct": distance_pct,
        }
    return {
        "verdict": "trend_far_ema",
        "label": f"🟡 По тренду, далеко от EMA20 ({signed_distance_pct:+.1f}%)",
        "distance_pct": distance_pct,
    }
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_trend.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd C:\crypto_bot
git add ema_trend.py tests/
git commit -m "feat: get_trend_verdict — вердикт по тренду и положению к EMA"
```

---

### Task 3: Настройка порога через `bot_settings.json`

**Files:**
- Modify: `C:\crypto_bot\crypto_bot.py:417-437` (`load_settings`, `save_settings`)
- Test: `C:\crypto_bot\tests\test_settings.py`

**Interfaces:**
- Consumes: `ema_trend.EMA_DISTANCE_THRESHOLD_PCT` (модульная переменная, Task 1)
- Produces: `load_settings()` и `save_settings()` в `crypto_bot.py` теперь читают/пишут `EMA_DISTANCE_THRESHOLD_PCT` наравне с существующими `MIN_VOLUME_USDT`, `PUMP_PCT`, `IIV_HOT`, `MIN_PAIR_AGE_DAYS`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_settings.py
import json
import crypto_bot
import ema_trend


def test_load_settings_reads_ema_threshold(tmp_path, monkeypatch):
    settings_file = tmp_path / "bot_settings.json"
    settings_file.write_text(json.dumps({"EMA_DISTANCE_THRESHOLD_PCT": 1.2}))
    monkeypatch.setattr(crypto_bot, "SETTINGS_FILE", str(settings_file))
    crypto_bot.load_settings()
    assert ema_trend.EMA_DISTANCE_THRESHOLD_PCT == 1.2


def test_save_settings_writes_ema_threshold(tmp_path, monkeypatch):
    settings_file = tmp_path / "bot_settings.json"
    monkeypatch.setattr(crypto_bot, "SETTINGS_FILE", str(settings_file))
    ema_trend.EMA_DISTANCE_THRESHOLD_PCT = 0.8
    crypto_bot.save_settings()
    saved = json.loads(settings_file.read_text())
    assert saved["EMA_DISTANCE_THRESHOLD_PCT"] == 0.8
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd C:\crypto_bot && python -m pytest tests/test_settings.py -v`
Expected: FAIL — `AssertionError` (значение не меняется, ключ ещё не читается/не пишется)

- [ ] **Step 3: Добавить импорт и обновить `load_settings`/`save_settings`**

Добавить в начало `crypto_bot.py` (после существующих импортов, перед `def fmt_caption`):

```python
import ema_trend
```

Заменить существующую функцию `load_settings` (строки 417-428):

```python
def load_settings():
    global MIN_VOLUME_USDT, PUMP_PCT, IIV_HOT, MIN_PAIR_AGE_DAYS
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
            MIN_VOLUME_USDT   = s.get("MIN_VOLUME_USDT",   MIN_VOLUME_USDT)
            PUMP_PCT          = s.get("PUMP_PCT",           PUMP_PCT)
            IIV_HOT           = s.get("IIV_HOT",            IIV_HOT)
            MIN_PAIR_AGE_DAYS = s.get("MIN_PAIR_AGE_DAYS",  MIN_PAIR_AGE_DAYS)
            ema_trend.EMA_DISTANCE_THRESHOLD_PCT = s.get(
                "EMA_DISTANCE_THRESHOLD_PCT", ema_trend.EMA_DISTANCE_THRESHOLD_PCT
            )
        log.info(f"Настройки загружены: vol=${MIN_VOLUME_USDT//1_000_000}M pump={PUMP_PCT}% iiv={IIV_HOT}x age={MIN_PAIR_AGE_DAYS//30}мес ema_dist={ema_trend.EMA_DISTANCE_THRESHOLD_PCT}%")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
```

Заменить существующую функцию `save_settings` (строки 430-437):

```python
def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump({
            "MIN_VOLUME_USDT":   MIN_VOLUME_USDT,
            "PUMP_PCT":          PUMP_PCT,
            "IIV_HOT":           IIV_HOT,
            "MIN_PAIR_AGE_DAYS": MIN_PAIR_AGE_DAYS,
            "EMA_DISTANCE_THRESHOLD_PCT": ema_trend.EMA_DISTANCE_THRESHOLD_PCT,
        }, f)
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd C:\crypto_bot && python -m pytest tests/test_settings.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd C:\crypto_bot
git add crypto_bot.py tests/test_settings.py
git commit -m "feat: EMA_DISTANCE_THRESHOLD_PCT в bot_settings.json"
```

---

### Task 4: `fetch_trend_verdict` — обёртка с сетевыми запросами и обработкой ошибок

**Files:**
- Modify: `C:\crypto_bot\crypto_bot.py` (добавить функцию после `get_klines`, строка ~196)
- Test: `C:\crypto_bot\tests\test_fetch_trend_verdict.py`

**Interfaces:**
- Consumes: `get_klines(symbol: str, interval: str, limit: int) -> list[dict]` (существующая, `crypto_bot.py:176`), `ema_trend.get_trend_verdict(...)` (Task 2), `ema_trend.TREND_INTERVAL`, `ema_trend.TREND_LIMIT`, `ema_trend.PULLBACK_INTERVAL`, `ema_trend.PULLBACK_LIMIT`
- Produces: `fetch_trend_verdict(symbol: str, direction: str, price: float) -> dict` — тот же формат что `get_trend_verdict`, плюс `{"verdict": "unknown", "label": "⚪ Не удалось проверить", "distance_pct": None}` при сетевой ошибке

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_fetch_trend_verdict.py
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
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd C:\crypto_bot && python -m pytest tests/test_fetch_trend_verdict.py -v`
Expected: FAIL — `AttributeError: module 'crypto_bot' has no attribute 'fetch_trend_verdict'`

- [ ] **Step 3: Добавить `fetch_trend_verdict` в `crypto_bot.py` сразу после `get_klines` (после строки 195, перед `def get_pair_age_days`)**

```python
def fetch_trend_verdict(symbol: str, direction: str, price: float) -> dict:
    """Обёртка над ema_trend.get_trend_verdict с сетевыми запросами и обработкой ошибок."""
    try:
        trend_candles    = get_klines(symbol, ema_trend.TREND_INTERVAL, ema_trend.TREND_LIMIT)
        pullback_candles = get_klines(symbol, ema_trend.PULLBACK_INTERVAL, ema_trend.PULLBACK_LIMIT)
        return ema_trend.get_trend_verdict(direction, price, trend_candles, pullback_candles)
    except Exception as e:
        log.warning(f"Не удалось получить тренд-вердикт для {symbol}: {e}")
        return {"verdict": "unknown", "label": "⚪ Не удалось проверить", "distance_pct": None}
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `cd C:\crypto_bot && python -m pytest tests/test_fetch_trend_verdict.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd C:\crypto_bot
git add crypto_bot.py tests/test_fetch_trend_verdict.py
git commit -m "feat: fetch_trend_verdict — сетевая обёртка с fallback на ошибку"
```

---

### Task 5: `fmt_caption` — добавить строку тренда в подпись

**Files:**
- Modify: `C:\crypto_bot\crypto_bot.py:29-41` (`fmt_caption`)
- Test: `C:\crypto_bot\tests\test_fmt_caption.py`

**Interfaces:**
- Produces: `fmt_caption(pair_name, signal_label, pump_pct, price_then, price_now, chg_24h, vol_str, funding=None, trend_label=None) -> str` — новый необязательный параметр `trend_label`, добавляет строку `"\nТренд: {trend_label}"` если передан

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_fmt_caption.py
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
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd C:\crypto_bot && python -m pytest tests/test_fmt_caption.py -v`
Expected: FAIL — `TypeError: fmt_caption() got an unexpected keyword argument 'trend_label'`

- [ ] **Step 3: Заменить `fmt_caption` (строки 29-41)**

```python
def fmt_caption(pair_name, signal_label, pump_pct, price_then, price_now, chg_24h, vol_str, funding=None, trend_label=None) -> str:
    """Формирует caption без конфликтов с Markdown."""
    funding_str = ""
    if funding is not None:
        emoji = "🟢" if funding > 0 else "🔴" if funding < 0 else "⚪"
        funding_str = f"\nФандинг: {emoji} {funding:+.4f}%"
    trend_str = f"\nТренд: {trend_label}" if trend_label else ""
    return (
        f"<code>{pair_name}</code>\n"
        f"{signal_label}: <b>{pump_pct:+.1f}%</b>\n"
        f"<code>{price_then:.5g}</code> → <code>{price_now:.5g}</code>\n"
        f"24h: {chg_24h:+.2f}%   Vol: {vol_str}"
        f"{funding_str}"
        f"{trend_str}"
    )
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `cd C:\crypto_bot && python -m pytest tests/test_fmt_caption.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd C:\crypto_bot
git add crypto_bot.py tests/test_fmt_caption.py
git commit -m "feat: fmt_caption — строка тренда в подписи сигнала"
```

---

### Task 6: Подключить `fetch_trend_verdict` в `process_kline`

**Files:**
- Modify: `C:\crypto_bot\crypto_bot.py` (внутри `process_kline`, между строками 690 и 711 в исходном файле)

**Interfaces:**
- Consumes: `fetch_trend_verdict(symbol, direction, price) -> dict` (Task 4), `fmt_caption(..., trend_label=...)` (Task 5)

Эта задача — только проводка между уже протестированными функциями внутри асинхронного цикла (`process_kline` дёргает реальный вебсокет/сеть), поэтому отдельного unit-теста нет — проверяется в Task 7 на реальном сигнале.

- [ ] **Step 1: Найти в `process_kline` строку `signal_label = "🚀 Pump" if "ПАМП" in desc else "💥 Dump"` и добавить сразу после неё вычисление направления и вердикта**

Было:

```python
        signal_label = "🚀 Pump" if "ПАМП" in desc else "💥 Dump"

        funding = g_funding_rates.get(symbol)

        caption = fmt_caption(
            pair_name, signal_label, pump_pct,
            price_then, price_now, chg_24h, vol_str, funding
        )
```

Стало:

```python
        signal_label = "🚀 Pump" if "ПАМП" in desc else "💥 Dump"
        direction = "long" if "ПАМП" in desc else "short"

        funding = g_funding_rates.get(symbol)
        trend_verdict = fetch_trend_verdict(symbol, direction, price_now)

        caption = fmt_caption(
            pair_name, signal_label, pump_pct,
            price_then, price_now, chg_24h, vol_str, funding,
            trend_label=trend_verdict["label"]
        )
```

- [ ] **Step 2: Прогнать полный набор тестов, убедиться что ничего не сломалось**

Run: `cd C:\crypto_bot && python -m pytest tests/ -v`
Expected: PASS (все тесты из Task 1-5, `process_kline` напрямую не тестируется)

- [ ] **Step 3: Commit**

```bash
cd C:\crypto_bot
git add crypto_bot.py
git commit -m "feat: подключить тренд-вердикт к алертам памп/дамп"
```

---

### Task 7: Деплой на сервер и проверка на реальном сигнале

**Files:**
- Нет новых файлов — деплой существующих: `crypto_bot.py`, `ema_trend.py` на `root@64.188.57.249:/root/`

- [ ] **Step 1: Скопировать файлы на сервер**

```bash
scp -i ~/.ssh/id_ed25519 C:/crypto_bot/crypto_bot.py C:/crypto_bot/ema_trend.py root@64.188.57.249:/root/
```

- [ ] **Step 2: Перезапустить systemd-сервис**

```bash
ssh -i ~/.ssh/id_ed25519 root@64.188.57.249 "systemctl restart crypto-bot && sleep 5 && systemctl status crypto-bot --no-pager -l"
```

Expected: `Active: active (running)`, в логе нет traceback после рестарта

- [ ] **Step 3: Проверить лог на чистый старт**

```bash
ssh -i ~/.ssh/id_ed25519 root@64.188.57.249 "tail -n 30 /root/bot.log"
```

Expected: строки `Настройки загружены: ... ema_dist=0.5%`, `Всего USDT-пар: ...`, без ошибок импорта `ema_trend`

- [ ] **Step 4: Дождаться реального сигнала и сверить вердикт вручную с TradingView**

Когда придёт следующий алерт в Telegram — открыть пару на TradingView (1H, EMA50/EMA200) и визуально сверить: совпадает ли направление тренда в боте с тем что на графике. Задача считается выполненной, если 2-3 подряд сигнала дают вердикт, совпадающий с ручной проверкой.

---

## Self-Review

- **Спека покрыта:** архитектура (Task 4/6), логика тренда/пуллбека (Task 2), вердикт 4 состояния (Task 2 — 3 тестируемых + `unknown` в Task 4), формат сообщения (Task 5), обработка ошибок (Task 4), тестирование (Tasks 1-5 unit, Task 7 ручная проверка). Скоринг/relative strength к BTC — явно вне рамок, не реализуем.
- **Плейсхолдеров нет** — весь код и тесты выписаны полностью.
- **Согласованность типов:** `get_trend_verdict` и `fetch_trend_verdict` возвращают одинаковый словарь `{"verdict", "label", "distance_pct"}` во всех задачах; `fmt_caption` принимает `trend_label: str | None` везде одинаково.
