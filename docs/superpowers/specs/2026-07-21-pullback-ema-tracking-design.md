# Довыставление контр-сигнала по мере пробоя недельных EMA

## Контекст

Контр-сигнал (`ema_pullback.py`) сейчас шлёт один сигнал сразу после памп/дамп-сигнала — лимитку на ближайшей к цене недельной EMA (обычно EMA7). На реальном примере LAUSDT (памп после сильного дампа, цена под всеми EMA) бот выдал сигнал именно на EMA7, и она совпала с хаем предыдущего движения — разумный уровень.

Идея: если цена пробивает EMA7 (закрытие дневной свечи выше — для лонга), это не отменяет сетап, а означает, что пора ждать отскока уже от следующей EMA (14, потом 28) — «ловим подход к EMA и выставляем лимитки на отскок от них» по мере прохождения каждой.

## Триггер продвижения / отмены

- **Продвижение к следующей EMA:** закрытие дневной свечи (D1) по ту сторону текущего входа, что и ожидаемое направление движения (выше входа — для лонга, ниже — для шорта).
- **Отмена трекинга:** закрытие дневной свечи по ту сторону стопа — структура сломана, дальше не ждём.
- **Естественное завершение:** пройдены все EMA (7→14→28) или дальше пробивать нечего (следующего периода нет / не хватает данных для его расчёта, либо для него нет реального недельного хая/лоя под стоп).

## Архитектура

Новая проверка встраивается в уже существующий `refresh_loop()` в `crypto_bot.py` (по аналогии с часовым обновлением фандинга и 12-часовым сбросом данных) — раз в сутки, по смене даты UTC, с запасом 00:05 UTC на закрытие дневной свечи. Отдельная asyncio-задача не нужна.

Состояние трекинга персистится в SQLite (`/root/signals.db`, та же БД, что и `signals`/`signal_checks`) — не в памяти процесса, потому что бот передеплоится почти каждую рабочую сессию (частые `systemctl restart`), а трекинг одной пары может тянуться неделями между закрытиями дневных свечей. In-memory состояние гарантированно потеряется на середине пути.

## Компоненты

### `ema_pullback.py` (чистая логика, без сети/БД/side-effects)

- Рефакторинг `build_pullback_signal`: вычисление входа/стопа/тейка для *конкретного* периода EMA выносится в

  ```python
  def build_pullback_signal_for_period(direction: str, price: float, weekly_candles: list[dict], entry_period: int) -> dict | None
  ```

  `build_pullback_signal` становится тонкой обёрткой — находит ближайший период на контр-стороне и делегирует в `build_pullback_signal_for_period`. Поведение и все существующие тесты (40/40) не меняются — это чистый рефакторинг без изменения логики.

- Новая чистая функция

  ```python
  def evaluate_tracking(direction: str, entry: float, stop: float, daily_close: float) -> str
  # -> "advance" | "invalidate" | "none"
  ```

  Решает по дневному закрытию, что делать — без сети и без похода за следующей EMA (это отдельный шаг в `crypto_bot.py`).

### `crypto_bot.py` (сеть, БД, Telegram — как сейчас)

- Новая таблица в `signals.db`:

  ```sql
  CREATE TABLE IF NOT EXISTS pullback_tracking (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT NOT NULL,
      pair_name TEXT NOT NULL,
      direction TEXT NOT NULL,       -- направление контр-сигнала: long/short
      entry_period INTEGER NOT NULL, -- текущий период EMA (7/14/28)
      entry REAL NOT NULL,
      stop REAL NOT NULL,
      take REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'active', -- active/invalidated/done
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
  )
  ```

  Функции `save_pullback_tracking`, `update_pullback_tracking`, `get_active_pullback_tracking` — по образцу существующих `save_signal`/`save_signal_check` (свой `sqlite3.connect` на вызов).

- `send_pullback_signal()`: после отправки начального контр-сигнала создаёт запись `pullback_tracking` со статусом `active` и `entry_period = pullback['entry_period']` — но только если для этой пары+направления ещё нет активной записи (не плодим дубликаты при повторных памп/дамп сигналах по той же паре).

- `refresh_loop()`: добавляется проверка смены даты UTC (по аналогии с `last_funding`/`last_full`). При смене даты (после 00:05 UTC) один раз вызывается `check_pullback_tracking()`.

- `check_pullback_tracking()`:
  1. Забирает все записи со статусом `active`.
  2. Для каждой — дневная свеча (`get_klines(symbol, '1d', 2)`) и свежие недельные свечи (`get_klines(symbol, ema_pullback.WEEKLY_INTERVAL, ema_pullback.WEEKLY_LIMIT)`).
  3. `ema_pullback.evaluate_tracking(direction, entry, stop, daily_close)`:
     - `"invalidate"` → статус `invalidated`, сообщение в Telegram «🔻 Сетап {symbol} сломан — дневное закрытие пробило стоп».
     - `"advance"` → берёт следующий период из `[7, 14, 28]` после текущего `entry_period`. Если периода нет (уже 28) → статус `done` + сообщение «🏁 {symbol} прошёл все EMA, дальше пробивать нечего». Если есть — `ema_pullback.build_pullback_signal_for_period(direction, price, weekly_candles, next_period)`; если `None` (не хватает структуры для стопа) → статус `done`; иначе обновляет запись (entry_period/entry/stop/take/updated_at) и шлёт новый контр-сигнал тем же форматом (`fmt_pullback_caption`), что и сейчас.
     - `"none"` → ничего не меняем.
  4. Сетевая ошибка при обработке одной пары → лог `warning`, статус не трогаем, пробуем на следующий день. Не роняем обработку остальных пар из-за одной ошибки.

## Тестирование

- `test_ema_pullback.py`: рефактор `build_pullback_signal` → `build_pullback_signal_for_period` не ломает ни один из текущих 40 тестов. Новые тесты на `build_pullback_signal_for_period` с явным периодом (14, 28) и на `evaluate_tracking` (advance/invalidate/none на синтетических entry/stop/daily_close, включая границы).
- Новый `test_pullback_tracking.py` (или расширение `test_fetch_pullback_signal.py`) — на выбор следующего периода по списку `[7,14,28]` и на сценарий «структуры не хватает → done», с замоканными klines. БД-функции (`save/update/get_active_pullback_tracking`) тестируются на временной SQLite-БД, по образцу существующих тестов `test_settings.py`/аналогичных для `signals`/`signal_checks`, если такие есть.

## Вне рамок

- Не отслеживаем сетапы, где начальный контр-сигнал вообще не выдался (структуры не было с самого начала) — трекинг начинается только вместе с первым сигналом.
- Не меняем логику самого начального контр-сигнала (выбор ближайшей EMA, стоп по недельному хаю/лою, санитарный потолок дистанции) — только добавляем продолжение поверх неё.
