# Гейт «свежий спайк» для контр-сигнала — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Контр-сигнал (`ema_pullback.build_pullback_signal`) не должен срабатывать, если в последних недельных свечах было экстремальное движение — сигнал по SYNUSDT (вертикальный памп 0.03→1.0 и обвал за 1-2 недели) должен возвращать `None`, легитимные сигналы (плавный тренд, ALICE-подобные) — проходить как раньше.

**Architecture:** Новая чистая функция `has_recent_spike(weekly_candles)` в `ema_pullback.py` проверяет "тело" последних `SPIKE_LOOKBACK_CANDLES` (2) недельных свечей (`abs(close-open)/open*100`) на превышение `SPIKE_BODY_PCT_THRESHOLD` (50.0). Вызывается в начале `build_pullback_signal`, до расчёта EMA — если сработал, сразу `return None`. Никаких новых сетевых запросов или полей в БД — используется то же поле `open`, что уже приходит из `get_klines()`.

**Tech Stack:** Python 3.11, pytest, numpy (без новых зависимостей).

## Global Constraints

- Спека: `docs/superpowers/specs/2026-07-23-pullback-spike-gate-design.md` — следовать точно, не отклоняться от критерия (последние 2 свечи, порог 50%, поле `open` из `weekly_candles`).
- `SPIKE_LOOKBACK_CANDLES = 2`, `SPIKE_BODY_PCT_THRESHOLD = 50.0` — точные значения, не менять без явного решения пользователя.
- Существующие 40/40 тестов в `test_ema_pullback.py` должны остаться зелёными.
- Деплой на TimeWeb (scp + `systemctl restart crypto-bot`) — только после того, как все тесты локально зелёные и пользователь подтвердил.

---

### Task 1: Обновить тестовый хелпер `make_weekly_candles` — добавить поле `open`

**Files:**
- Modify: `tests/test_ema_pullback.py:11-21` (функция `make_weekly_candles`)
- Test: `tests/test_ema_pullback.py` (существующие тесты используют этот хелпер)

**Interfaces:**
- Produces: `make_weekly_candles(closes: list[float], overrides: dict[int, dict] | None = None) -> list[dict]` — теперь каждый candle-dict содержит ключи `close`, `high`, `low`, **и `open`**. По умолчанию `open == close` этой же свечи (тело 0%, не создаёт ложного спайка). `overrides[i]` может задавать `"open"` явно, как уже задаются `"high"`/`"low"`.

- [ ] **Step 1: Написать/обновить тест, проверяющий что `open` присутствует и по умолчанию равен `close`**

Добавить в конец `tests/test_ema_pullback.py`:

```python
def test_make_weekly_candles_default_open_equals_close():
    candles = make_weekly_candles([1.0, 2.0, 3.0])
    assert all(c["open"] == c["close"] for c in candles)


def test_make_weekly_candles_open_override():
    candles = make_weekly_candles([1.0, 2.0, 3.0], overrides={1: {"open": 0.5}})
    assert candles[1]["open"] == 0.5
    assert candles[0]["open"] == candles[0]["close"]
```

- [ ] **Step 2: Запустить тесты, убедиться что падают (KeyError или AssertionError на отсутствии `open`)**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_pullback.py -k "make_weekly_candles" -v`
Expected: FAIL — `KeyError: 'open'` (хелпер пока не добавляет `open` в candle-dict)

- [ ] **Step 3: Обновить `make_weekly_candles` — добавить `open` в каждый candle-dict**

Заменить функцию (`tests/test_ema_pullback.py:11-21`) на:

```python
def make_weekly_candles(closes: list[float], overrides: dict[int, dict] | None = None) -> list[dict]:
    """closes — для расчёта EMA; overrides — {индекс: {"high": v, "low": v, "open": v}} для конкретных
    недельных свечей. По умолчанию high/low выставлены так, чтобы никогда не попасть в поиск
    ближайшего уровня дальше входа (короткого high << любого входа, длинного low >> любого входа) —
    в тесте "видны" только явно заданные overrides. По умолчанию open == close (тело свечи 0%,
    не триггерит гейт свежего спайка)."""
    overrides = overrides or {}
    candles = []
    for i, c in enumerate(closes):
        ov = overrides.get(i, {})
        candles.append({
            "open": ov.get("open", c),
            "close": c,
            "high": ov.get("high", -1e9),
            "low": ov.get("low", 1e9),
        })
    return candles
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_pullback.py -v`
Expected: PASS — все тесты зелёные (включая два новых и все существующие 40, которые теперь получают `open == close` по умолчанию через тот же хелпер)

- [ ] **Step 5: Commit**

```bash
cd C:\crypto_bot
git add tests/test_ema_pullback.py
git commit -m "test: добавить поле open в тестовый хелпер make_weekly_candles"
```

---

### Task 2: Реализовать `has_recent_spike` и подключить гейт в `build_pullback_signal`

**Files:**
- Modify: `ema_pullback.py` (добавить константы и функцию, подключить гейт в `build_pullback_signal`)
- Test: `tests/test_ema_pullback.py`

**Interfaces:**
- Consumes: `make_weekly_candles` из Task 1 (уже поддерживает `open` через overrides)
- Produces: `has_recent_spike(weekly_candles: list[dict]) -> bool`, константы `SPIKE_LOOKBACK_CANDLES = 2`, `SPIKE_BODY_PCT_THRESHOLD = 50.0` в `ema_pullback.py` — доступны для импорта в тестах как `from ema_pullback import has_recent_spike, SPIKE_LOOKBACK_CANDLES, SPIKE_BODY_PCT_THRESHOLD`

- [ ] **Step 1: Написать падающие тесты на `has_recent_spike`**

Добавить в `tests/test_ema_pullback.py`:

```python
from ema_pullback import has_recent_spike, SPIKE_LOOKBACK_CANDLES, SPIKE_BODY_PCT_THRESHOLD


def test_has_recent_spike_true_when_last_candle_body_exceeds_threshold():
    # Реальный кейс SYN/USDT: последняя свеча — резкий обвал с 1.0 до 0.15 (тело 85%)
    candles = make_weekly_candles([0.2] * 10, overrides={9: {"open": 1.0}})
    candles[9]["close"] = 0.15
    assert has_recent_spike(candles) is True


def test_has_recent_spike_true_when_second_to_last_candle_is_the_spike():
    # Свеча пампа — предпоследняя (open=0.03, close=1.0, тело >3000%), последняя — обычная
    candles = make_weekly_candles([0.2] * 10, overrides={8: {"open": 0.03}})
    candles[8]["close"] = 1.0
    assert has_recent_spike(candles) is True


def test_has_recent_spike_false_when_spike_is_old():
    # Тот же скачок, но 3 свечи назад — вне окна SPIKE_LOOKBACK_CANDLES (2)
    candles = make_weekly_candles([0.2] * 10, overrides={6: {"open": 0.03}})
    candles[6]["close"] = 1.0
    assert has_recent_spike(candles) is False


def test_has_recent_spike_false_for_smooth_trend():
    # Плавный тренд — open == close по умолчанию, тело 0%
    candles = make_weekly_candles(list(np.linspace(1.0, 2.0, 10)))
    assert has_recent_spike(candles) is False


def test_has_recent_spike_false_exactly_at_threshold():
    # Тело ровно на пороге (не строго больше) — не считается спайком
    candles = make_weekly_candles([0.2] * 10, overrides={9: {"open": 1.0}})
    candles[9]["close"] = 0.5  # тело = 50.0%, ровно порог
    assert has_recent_spike(candles) is False
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_pullback.py -k "has_recent_spike" -v`
Expected: FAIL — `ImportError: cannot import name 'has_recent_spike'`

- [ ] **Step 3: Реализовать константы и функцию в `ema_pullback.py`**

Добавить после блока констант в начале файла (после `STOP_MAX_DISTANCE_FACTOR` и `EMA_WARMUP_FACTOR`, перед `def calc_weekly_emas`):

```python
# Гейт свежего спайка. Реальный пример SYN/USDT: вертикальный памп 0.03 → ~1.0 и обратный обвал
# в пределах 1-2 недель — структура (EMA, "видимые" хай/лои) ещё не устаканилась после разового
# экстремального движения, хотя формально EMA и стоп находятся. Порядок EMA не различает такой
# спайк от плавного тренда (проверено численно на синтетических данных), поэтому проверяем
# напрямую тело последних свечей.
SPIKE_LOOKBACK_CANDLES = 2
SPIKE_BODY_PCT_THRESHOLD = 50.0


def has_recent_spike(weekly_candles: list[dict]) -> bool:
    """True — если тело (|close-open|/open) любой из последних SPIKE_LOOKBACK_CANDLES свечей
    превышает SPIKE_BODY_PCT_THRESHOLD. Ловит и саму свечу пампа/дампа, и следующую свечу отката —
    обе бывают экстремальными сразу после события."""
    recent = weekly_candles[-SPIKE_LOOKBACK_CANDLES:]
    return any(
        abs(c["close"] - c["open"]) / c["open"] * 100 > SPIKE_BODY_PCT_THRESHOLD
        for c in recent
    )
```

- [ ] **Step 4: Запустить тесты на `has_recent_spike`, убедиться что проходят**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_pullback.py -k "has_recent_spike" -v`
Expected: PASS — все 5 новых тестов зелёные

- [ ] **Step 5: Подключить гейт в `build_pullback_signal`**

В `ema_pullback.py`, найти начало `build_pullback_signal` (сейчас начинается с `closes = np.array(...)`). Добавить проверку самой первой строкой тела функции:

```python
def build_pullback_signal(direction: str, price: float, weekly_candles: list[dict]) -> dict | None:
    """direction — направление ИСХОДНОГО памп/дамп сигнала ("long" на пампе, "short" на дампе).
    Контр-сигнал открывается в противоположную сторону. None — если недельной структуры
    (EMA для входа или реального хая/лоя дальше входа для стопа) не хватает, либо если в
    последних свечах был свежий спайк (структура ещё не устаканилась)."""
    if has_recent_spike(weekly_candles):
        return None

    closes = np.array([c["close"] for c in weekly_candles])
    emas = calc_weekly_emas(closes)
    ...
```

(оставить весь остальной код функции без изменений — только добавить две новые строки в начале и обновить docstring, как показано)

- [ ] **Step 6: Написать интеграционный тест — спайк отменяет весь сигнал целиком**

Добавить в `tests/test_ema_pullback.py`:

```python
def test_build_pullback_signal_none_when_recent_spike():
    # SYN-подобный сценарий: долгий даунтренд, затем на последней неделе резкий памп-обвал —
    # даже если формально EMA и стоп нашлись бы, гейт спайка должен отменить сигнал целиком.
    closes = list(np.linspace(2.0, 1.0, 99)) + [1.0]
    candles = make_weekly_candles(closes, {
        0: {"low": 0.5},
        98: {"open": 0.03},
    })
    candles[98]["close"] = 1.0  # последняя свеча: open=0.03, close=1.0 — тело >3000%

    result = build_pullback_signal("short", price=1.10, weekly_candles=candles)
    assert result is None
```

- [ ] **Step 7: Запустить тесты, убедиться что проходят**

Run: `cd C:\crypto_bot && python -m pytest tests/test_ema_pullback.py -v`
Expected: PASS — все тесты зелёные

- [ ] **Step 8: Commit**

```bash
cd C:\crypto_bot
git add ema_pullback.py tests/test_ema_pullback.py
git commit -m "feat: гейт свежего спайка в контр-сигнале — не шлём после резкого пампа/обвала (SYN)"
```

---

### Task 3: Прогнать полный набор тестов и задеплоить на TimeWeb

**Files:**
- None (только запуск и деплой существующих файлов `ema_pullback.py`)

**Interfaces:**
- Consumes: финальный `ema_pullback.py` из Task 2

- [ ] **Step 1: Прогнать весь набор тестов проекта**

Run: `cd C:\crypto_bot && python -m pytest -v`
Expected: PASS — все тесты зелёные (было 40 в `test_ema_pullback.py` + новые из Task 1/2, плюс остальные test-файлы проекта без изменений)

- [ ] **Step 2: Задеплоить `ema_pullback.py` на TimeWeb**

Run: `scp -i ~/.ssh/id_ed25519 C:\crypto_bot\ema_pullback.py root@64.188.57.249:/root/ema_pullback.py`
Expected: файл скопирован без ошибок

- [ ] **Step 3: Перезапустить сервис и проверить чистый старт по логам**

Run: `ssh -i ~/.ssh/id_ed25519 root@64.188.57.249 "systemctl restart crypto-bot && sleep 3 && journalctl -u crypto-bot -n 30 --no-pager"`
Expected: в логе нет traceback/ошибок импорта, бот стартовал штатно (как и в предыдущих деплоях этого файла)

- [ ] **Step 4: Commit (если после запуска понадобились правки — иначе шаг пропускается, Task 2 уже закоммитил финальный код)**

Деплой не создаёт новых файлов в git — коммитить нечего, если Step 1-3 прошли без правок кода.
