"""
Telegram Crypto Signal Bot — ВСЕ пары /USDT на Binance
Сигналы:
  1. Цена пробивает тренд + дисбаланс объёмов
  2. Памп/Дамп: цена изменилась на 6%+ за PUMP_WINDOW свечей
Фильтры: возраст пары >= 6 мес, объём >= $1 млн за 24ч
Данные: Binance public API (ключи не нужны)
"""

import asyncio
import logging
import os
import time
import json
import sqlite3
from datetime import datetime, timezone, timedelta

import requests
import websockets
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

def format_pair_name(symbol: str) -> str:
    """Единый формат для всех мест, где пара пишется в БД (signals.pair_name,
    pullback_tracking.pair_name) или показывается человеку — без слэша,
    как в нативном формате Binance (SYMBOLUSDT)."""
    return symbol

def fmt_caption(pair_name, signal_label, pump_pct, price_then, price_now, chg_24h, vol_str, funding=None, trend_label=None) -> str:
    """Формирует caption без конфликтов с Markdown."""
    funding_str = ""
    if funding is not None:
        emoji = "🟢" if funding > 0 else "🔴" if funding < 0 else "⚪"
        funding_str = f"\nФандинг: {emoji} {funding:+.4f}%"
    trend_str = f"\nТренд: {trend_label}" if trend_label else ""
    return (
        f"<code>{pair_name}</code>  <i>Binance</i>\n"
        f"{signal_label}: <b>{pump_pct:+.1f}%</b>\n"
        f"<code>{price_then:.5g}</code> → <code>{price_now:.5g}</code>\n"
        f"24h: {chg_24h:+.2f}%   Vol: {vol_str}"
        f"{funding_str}"
        f"{trend_str}"
    )


def fmt_entry_caption(pair_name: str, direction: str, entry_price: float, stop_price: float,
                       take_price: float | None, pattern_kind: str, pattern_time: datetime,
                       pump_pct: float, pump_window_min: int) -> str:
    """Caption для входа после разворотного паттерна (пин-бар/поглощение) — разворот
    против исходного памп/дампа. take_price — уровень начала движения (обычно цена
    откатывает как минимум туда); None, если движение уже откатило дальше этого уровня
    к моменту входа — тогда цель пропускаем, решать руками."""
    direction_label = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    move_label = "дампа" if direction == "long" else "пампа"
    time_str = pattern_time.strftime("%H:%M UTC")
    take_line = (
        f"Тейк: <code>{take_price:.5g}</code> (начало движения)\n"
        if take_price is not None else
        "Тейк: цель уже пройдена, дальше на своё усмотрение\n"
    )
    return (
        f"🎯 Вход <code>{pair_name}</code>  <i>Binance</i>  {direction_label}\n"
        f"{pattern_kind.capitalize()} {time_str} после {move_label} {pump_pct:+.2f}% за {pump_window_min} мин\n"
        f"+ дивергенция RSI подтверждена\n"
        f"Вход: <code>{entry_price:.5g}</code>\n"
        f"Стоп: <code>{stop_price:.5g}</code> (за хвост/тело сигнальной свечи)\n"
        f"{take_line}"
    )

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = "8892073473:AAFe1oVfpGXRTh_SHEoL7UD_OsYvTyho2SA"   # вставь токен от @BotFather
CHAT_ID        = "-1003708330324"      # вставь chat_id куда слать сигналы

INTERVAL         = "5m"                   # 5-минутные свечи — сигнал берём с 5м графика, не с 1м
INTERVAL_MINUTES = int(INTERVAL.rstrip("m"))  # длительность одной свечи в минутах, единый источник для окон ниже
LOOKBACK       = (2 * 60) // INTERVAL_MINUTES  # сколько свечей на графике (2 часа)
CHECK_EVERY    = 60                       # проверка каждые 60 секунд
PAIR_DELAY     = 0.5                      # пауза между парами (сек)

# Параметры сигналов
TREND_PERIOD       = 60 // INTERVAL_MINUTES  # период скользящей средней (тренд), 60 минут
VOLUME_RATIO_MIN   = 2.0       # дисбаланс buy/sell для сигнала
PRICE_CHANGE_PCT   = 0.3       # % пробоя тренда

# Памп/Дамп
PUMP_PCT           = 6.0       # % изменения цены для сигнала
PUMP_WINDOW_MINUTES = 20       # за сколько минут считать памп — 4 свечи по 5м, явное ускорение, не растянутый час
PUMP_WINDOW         = PUMP_WINDOW_MINUTES // INTERVAL_MINUTES  # то же самое в свечах текущего таймфрейма

# Проверка сигналов постфактум — отследить, пошла ли цена в сторону сигнала
SIGNAL_CHECK_MINUTES       = [15, 60, 240]   # через сколько минут проверять цену
SIGNAL_CHECK_DEADZONE_PCT  = 0.5             # % — в пределах этого считаем "около входа"
SIGNAL_CHECK_STALE_MINUTES = 2 * INTERVAL_MINUTES  # свеча старше этого — считаем данные протухшими (запас в 2 свечи)

# Фильтры
MIN_PAIR_AGE_DAYS  = 180        # минимальный возраст пары (6 мес)
MIN_VOLUME_USDT    = 10_000_000 # минимальный объём за 24ч в USDT ($10 млн)

# RSI
RSI_PERIOD         = 14         # период RSI

# Фандинг-фильтр: пропускаем сигнал если лонги/шорты перегружены
FUNDING_FILTER     = True       # включить/выключить фильтр
FUNDING_MAX_LONG   = 0.15       # % выше этого — пропускаем памп (лонги перегружены)
FUNDING_MAX_SHORT  = -0.10      # % ниже этого — пропускаем дамп (шорты перегружены)

# Чёрный список — эти пары игнорируем
BLACKLIST = {
    # Стейблкоины
    "BUSDUSDT", "USDCUSDT", "TUSDUSDT", "USDPUSDT", "FDUSDUSDT",
    "DAIUSDT", "FRAXUSDT", "USTUSDT", "EURUSDT", "GBPUSDT",
    # Обёрнутые токены
    "WBTCUSDT", "WBNBUSDT", "WETHUSDT",
}

# Паттерны для исключения (токены с плечом)
BLACKLIST_PATTERNS = ["UP", "DOWN", "BULL", "BEAR"]


def calc_rsi(closes: np.ndarray, period: int = RSI_PERIOD) -> float:
    """RSI на основе numpy, без сторонних библиотек."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss < 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def calc_rsi_series(closes: np.ndarray, period: int = RSI_PERIOD) -> np.ndarray:
    """RSI на каждую свечу (скользящий) — NaN там, где данных ещё не хватает."""
    rsi_series = np.full(len(closes), np.nan)
    for i in range(period, len(closes)):
        rsi_series[i] = calc_rsi(closes[:i + 1], period)
    return rsi_series


def calc_iiv(total_vols: np.ndarray) -> float:
    """IIV — насколько текущий объём выше исторического среднего за IIV_PERIOD свечей."""
    if len(total_vols) < IIV_PERIOD + 1:
        return 0.0
    avg_vol = total_vols[-IIV_PERIOD-1:-1].mean()
    current_vol = total_vols[-1]
    if avg_vol < 1e-9:
        return 0.0
    return current_vol / avg_vol


def classify_signal_outcome(direction: str, entry_price: float, price_now: float,
                             deadzone_pct: float = SIGNAL_CHECK_DEADZONE_PCT) -> tuple[float, str]:
    """Сравнить цену с ценой входа и оценить, пошёл ли рынок в сторону сигнала.
    direction: "long" (памп, ждём роста) или "short" (дамп, ждём падения)."""
    raw_pct = (price_now - entry_price) / entry_price * 100
    adjusted_pct = raw_pct if direction == "long" else -raw_pct
    if adjusted_pct > deadzone_pct:
        verdict = "🟢 в плюс"
    elif adjusted_pct < -deadzone_pct:
        verdict = "🔴 в минус"
    else:
        verdict = "⚪ около входа (б/у)"
    return adjusted_pct, verdict


def is_blacklisted(symbol: str) -> bool:
    """True если пара в чёрном списке или содержит запрещённый паттерн."""
    if symbol in BLACKLIST:
        return True
    base = symbol.replace("USDT", "")
    for pattern in BLACKLIST_PATTERNS:
        if pattern in base:
            return True
    return False

# ─── ЛОГ СИГНАЛОВ (SQLite, для анализа) ────────────────────────────────────────

SIGNALS_DB = "/root/signals.db"


def init_signals_db(db_path: str = SIGNALS_DB) -> None:
    """Создать таблицы signals/signal_checks, если их ещё нет.
    pullback_tracking больше не создаём — контр-сигнал по недельным EMA убран
    (старая таблица и данные в БД остаются нетронутыми, просто не растут дальше)."""
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
    conn.commit()
    conn.close()


def save_signal(symbol: str, pair_name: str, direction: str, entry_price: float,
                 signal_time: datetime, trend_label: str | None,
                 db_path: str = SIGNALS_DB) -> int:
    """Записать новый сигнал, вернуть его id. trend_label здесь переиспользован под тип
    сигнала: None — сырой памп/дамп рывок (просто наблюдение), иначе — название разворотного
    паттерна ("поглощение"/"пин-бар"/...), которым подтверждён вход. Так дашборд статистики
    сможет отличить один тип от другого."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO signals (symbol, pair_name, direction, entry_price, signal_time, trend_label) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, pair_name, direction, entry_price, signal_time.isoformat(), trend_label)
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def save_signal_check(signal_id: int, minutes: int, pct: float | None, verdict: str | None,
                       db_path: str = SIGNALS_DB) -> None:
    """Записать результат проверки сигнала через N минут."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signal_checks (signal_id, minutes, pct, verdict) VALUES (?, ?, ?, ?)",
        (signal_id, minutes, pct, verdict)
    )
    conn.commit()
    conn.close()


# ─── ЛОГИРОВАНИЕ ──────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── BINANCE API ──────────────────────────────────────────────────────────────

BASE = "https://api.binance.com"

BASE_FUTURES = "https://fapi.binance.com"

def get_all_usdt_symbols() -> list[str]:
    """Получить все активные спот-пары с USDT на Binance."""
    r = requests.get(f"{BASE}/api/v3/exchangeInfo", timeout=15)
    r.raise_for_status()
    symbols = []
    for s in r.json()["symbols"]:
        if (s["quoteAsset"] == "USDT"
                and s["status"] == "TRADING"
                and s["isSpotTradingAllowed"]):
            symbols.append(s["symbol"])
    return symbols


def get_24h_tickers() -> dict[str, dict]:
    """Получить 24ч статистику для всех пар за один запрос."""
    r = requests.get(f"{BASE}/api/v3/ticker/24hr", timeout=15)
    r.raise_for_status()
    return {t["symbol"]: t for t in r.json()}


def get_funding_rates() -> dict[str, float]:
    """Получить текущие ставки фандинга с фьючерсного рынка Binance."""
    try:
        r = requests.get(f"{BASE_FUTURES}/fapi/v1/premiumIndex", timeout=10)
        r.raise_for_status()
        result = {}
        for item in r.json():
            sym = item.get("symbol", "")
            if sym.endswith("USDT"):
                try:
                    result[sym] = float(item.get("lastFundingRate", 0)) * 100  # в %
                except Exception:
                    pass
        return result
    except Exception as e:
        log.warning(f"Ошибка получения фандинга: {e}")
        return {}


def get_klines(symbol: str, interval: str, limit: int, timeout: int = 10) -> list[dict]:
    """Получить свечи с Binance."""
    r = requests.get(
        f"{BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=timeout
    )
    r.raise_for_status()
    candles = []
    for c in r.json():
        candles.append({
            "time":     datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc),
            "open":     float(c[1]),
            "high":     float(c[2]),
            "low":      float(c[3]),
            "close":    float(c[4]),
            "vol_sell": float(c[5]) - float(c[9]),
            "vol_buy":  float(c[9]),
        })
    return candles


def get_pair_age_days(symbol: str) -> float:
    """Возраст пары в днях."""
    r = requests.get(
        f"{BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": "1d", "limit": 1, "startTime": 0},
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return 0
    return (time.time() - data[0][0] / 1000) / 86400


# Кэш возраста пар
_pair_age_cache: dict[str, tuple[float, float]] = {}

def is_pair_old_enough(symbol: str) -> bool:
    now = time.time()
    if symbol in _pair_age_cache:
        age, checked_at = _pair_age_cache[symbol]
        if now - checked_at < 86400:
            return age >= MIN_PAIR_AGE_DAYS
    try:
        age = get_pair_age_days(symbol)
    except Exception:
        return False
    _pair_age_cache[symbol] = (age, now)
    return age >= MIN_PAIR_AGE_DAYS


def has_enough_volume(ticker: dict) -> bool:
    """Проверить что объём торгов за 24ч >= MIN_VOLUME_USDT."""
    try:
        return float(ticker.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
    except Exception:
        return False

# IIV — Increase In Volume (индикатор роста объёма)
IIV_PERIOD         = 20        # период для расчёта среднего объёма
IIV_HOT            = 5.0       # порог — объём в X раз выше среднего = сигнал

# "Тягучие" пампы/дампы (движение растянуто без всплеска объёма в одной свече, как ALICE 20.07):
# если цена ушла в STRONG_PUMP_MULTIPLIER раз дальше базового порога — шлём сигнал и без IIV-спайка
STRONG_PUMP_MULTIPLIER = 2.0

# ─── СИГНАЛ ───────────────────────────────────────────────────────────────────

class SignalState:
    def __init__(self):
        self.last_signal_time  = 0
        self.last_signal_price = 0.0   # цена на момент последнего сигнала
        self.cooldown          = 300   # минимум 5 мин между сигналами

    def check(self, candles: list[dict], funding: float | None = None) -> tuple[bool, str, float]:
        if time.time() - self.last_signal_time < self.cooldown:
            return False, "", 50.0

        closes     = np.array([c["close"] for c in candles])
        total_vols = np.array([c["vol_buy"] + c["vol_sell"] for c in candles])
        price      = closes[-1]

        rsi = calc_rsi(closes)

        # IIV — аномалия объёма за IIV_PERIOD свечей
        iiv           = calc_iiv(total_vols)
        has_vol_spike = iiv >= IIV_HOT

        # Памп/Дамп 6%+ от начала окна
        window    = min(PUMP_WINDOW, len(closes) - 1)
        price_ago = closes[-window - 1]
        pump_pct  = (price - price_ago) / price_ago * 100

        # Тягучее движение без спайка объёма — пропускаем через IIV-гейт,
        # если цена ушла вдвое дальше базового порога
        is_strong_move = abs(pump_pct) >= PUMP_PCT * STRONG_PUMP_MULTIPLIER

        # Динамический порог: если уже был сигнал — считаем % от той цены
        if self.last_signal_price > 0:
            pct_from_last = (price - self.last_signal_price) / self.last_signal_price * 100
            if abs(pct_from_last) < PUMP_PCT:
                return False, "", rsi

        if pump_pct >= PUMP_PCT and (has_vol_spike or is_strong_move):
            # Фандинг-фильтр: пропускаем памп если лонги перегружены
            if FUNDING_FILTER and funding is not None and funding > FUNDING_MAX_LONG:
                log.info(f"Памп пропущен (фандинг {funding:+.4f}% > {FUNDING_MAX_LONG}%)")
                return False, "", rsi
            rsi_label = f" | RSI {rsi:.0f}{'⚠️' if rsi > 75 else ''}"
            desc = (
                f"🚀 ПАМП\n"
                f"Рост +{pump_pct:.2f}% за {window * INTERVAL_MINUTES} мин\n"
                f"{price_ago:,.5g} → {price:,.5g}\n"
                f"IIV x{iiv:.1f} (порог {IIV_HOT}x){rsi_label}"
            )
            self.last_signal_time  = time.time()
            self.last_signal_price = price
            return True, desc, rsi

        if pump_pct <= -PUMP_PCT and (has_vol_spike or is_strong_move):
            # Фандинг-фильтр: пропускаем дамп если шорты перегружены
            if FUNDING_FILTER and funding is not None and funding < FUNDING_MAX_SHORT:
                log.info(f"Дамп пропущен (фандинг {funding:+.4f}% < {FUNDING_MAX_SHORT}%)")
                return False, "", rsi
            rsi_label = f" | RSI {rsi:.0f}{'⚠️' if rsi < 25 else ''}"
            desc = (
                f"💥 ДАМП\n"
                f"Падение {pump_pct:.2f}% за {window * INTERVAL_MINUTES} мин\n"
                f"{price_ago:,.5g} → {price:,.5g}\n"
                f"IIV x{iiv:.1f} (порог {IIV_HOT}x){rsi_label}"
            )
            self.last_signal_time  = time.time()
            self.last_signal_price = price
            return True, desc, rsi

        return False, "", rsi

# ─── ПИН-БАР ПОСЛЕ РЫВКА (вход на разворот) ────────────────────────────────────
# После памп/дамп-сигнала не входим сразу, а ждём пин-бар — свечу с длинным
# хвостом отказа в противоположную сторону от рывка. Памп вверх -> ждём
# медвежий пин-бар (длинный верхний хвост) -> вход в шорт. Дамп вниз -> ждём
# бычий пин-бар (длинный нижний хвост) -> вход в лонг.

PIN_BAR_WATCH_CANDLES  = 6     # сколько свечей ждём пин-бар после сигнала (30 мин на 5м графике)
PIN_BAR_WICK_MULT      = 2.0   # хвост пин-бара минимум во столько раз длиннее тела свечи
PIN_BAR_WICK_SHARE_MIN = 0.6   # и минимум такая доля всего диапазона свечи (high-low)

MIN_RISK_REWARD = 3.0   # минимум R:R (тейк/стоп от входа), иначе вход не публикуем — решение Вячеслава 10.09.2026


def is_pin_bar(candle: dict, bearish: bool) -> bool:
    """bearish=True — длинный верхний хвост (отказ сверху, сигнал на шорт после пампа).
    bearish=False — длинный нижний хвост (отказ снизу, сигнал на лонг после дампа)."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    total_range = h - l
    if total_range <= 0:
        return False
    body = abs(c - o)
    wick = (h - max(o, c)) if bearish else (min(o, c) - l)
    return wick >= PIN_BAR_WICK_MULT * body and wick >= PIN_BAR_WICK_SHARE_MIN * total_range


def is_engulfing(prev_candle: dict, candle: dict, bearish: bool) -> bool:
    """bearish=True — медвежье поглощение: текущая свеча красная и её тело полностью
    перекрывает тело предыдущей зелёной свечи (отказ сверху, сигнал на шорт после пампа).
    bearish=False — бычье поглощение, симметрично (сигнал на лонг после дампа)."""
    p_o, p_c = prev_candle["open"], prev_candle["close"]
    o, c = candle["open"], candle["close"]
    if bearish:
        return p_c > p_o and c < o and o >= p_c and c <= p_o
    return p_c < p_o and c > o and o <= p_c and c >= p_o


def combine_candles(candles: list[dict]) -> dict:
    """Схлопывает несколько подряд идущих свечей в одну синтетическую: open первой,
    close последней, high/low — экстремумы всех. Нужно для пин-бара, растянутого
    на пару свечей вместо одной."""
    return dict(
        open=candles[0]["open"], close=candles[-1]["close"],
        high=max(c["high"] for c in candles), low=min(c["low"] for c in candles),
    )


PIN_BAR_COMBINE_MAX = 3   # схлопываем до стольки последних свечей, если разворот растянут на 10-15 мин


def find_reversal_pattern(candles: list[dict], since_signal: list[dict], bearish: bool) -> dict | None:
    """Ищет разворотный паттерн: поглощение или пин-бар (одиночный или схлопнутый).
    candles — вся история свечей пары (для одиночного пин-бара/поглощения — смотрим на
    самый свежий конец). since_signal — свечи от пика рывка (включительно) до текущего
    момента, максимум PIN_BAR_COMBINE_MAX штук: схлопнутый пин-бар всегда привязан к пику,
    а не к «последним N свечам от сейчас» — иначе окно уезжает от пика, и фигура, сложившаяся
    прямо на развороте, не поймается через несколько свечей.
    Возвращает {"kind": подпись для сообщения, "candle": итоговая свеча для входа/стопа}
    или None, если ничего не подошло."""
    if len(candles) >= 2 and is_engulfing(candles[-2], candles[-1], bearish):
        return {"kind": "поглощение", "candle": candles[-1]}
    if is_pin_bar(candles[-1], bearish):
        return {"kind": "пин-бар", "candle": candles[-1]}
    for n in range(2, len(since_signal) + 1):
        combo = combine_candles(since_signal[:n])
        if is_pin_bar(combo, bearish):
            return {"kind": f"пин-бар за {n * INTERVAL_MINUTES} мин", "candle": combo}
    return None


DIVERGENCE_SWING_ORDER = 2   # соседей с каждой стороны, чтобы точка считалась локальным экстремумом


def has_rsi_divergence(candles: list[dict], bearish: bool) -> bool:
    """Дивергенция цена/RSI: сравниваем текущую свечу (последнюю в candles) с предыдущим
    локальным экстремумом того же типа. bearish=True — медвежья дивергенция: цена сейчас
    выше (или на уровне) прошлого максимума, а RSI ниже, чем был там — импульс слабее,
    несмотря на более высокую цену (сигнал на шорт). bearish=False — симметрично для
    минимума цены и лонга."""
    closes = np.array([c["close"] for c in candles])
    if len(closes) < RSI_PERIOD + 10:
        return False
    rsi_series = calc_rsi_series(closes)

    from scipy.signal import argrelextrema
    extrema_kind = np.greater if bearish else np.less
    try:
        # исключаем последние 3 свечи — это и есть текущее движение, ищем экстремум до него
        idx = argrelextrema(closes[:-3], extrema_kind, order=DIVERGENCE_SWING_ORDER)[0]
    except Exception:
        return False
    idx = idx[idx >= RSI_PERIOD]  # там, где RSI уже посчитан
    if len(idx) == 0:
        return False
    prev_idx = idx[-1]   # ближайший к текущему моменту прошлый экстремум

    cur_price, cur_rsi   = closes[-1], rsi_series[-1]
    prev_price, prev_rsi = closes[prev_idx], rsi_series[prev_idx]
    if np.isnan(cur_rsi) or np.isnan(prev_rsi):
        return False

    if bearish:
        return cur_price >= prev_price and cur_rsi < prev_rsi
    return cur_price <= prev_price and cur_rsi > prev_rsi

# ─── ГРАФИК ───────────────────────────────────────────────────────────────────

def build_chart(symbol: str, candles: list[dict], ticker: dict, signal_desc: str, rsi_val: float = 50.0) -> str:
    times  = [c["time"] for c in candles]
    closes = np.array([c["close"] for c in candles])
    buys   = np.array([c["vol_buy"] for c in candles])
    sells  = np.array([c["vol_sell"] for c in candles])

    # RSI для каждой свечи (скользящий)
    rsi_series = calc_rsi_series(closes)

    levels       = np.quantile(closes, [0.1, 0.25, 0.5, 0.75, 0.9])
    price_change = (closes[-1] - closes[0]) / closes[0] * 100

    from scipy.signal import argrelextrema
    try:
        local_max_idx = argrelextrema(closes, np.greater, order=5)[0]
        local_min_idx = argrelextrema(closes, np.less,    order=5)[0]
        extrema_idx   = np.concatenate([local_max_idx, local_min_idx])
    except Exception:
        extrema_idx = np.array([], dtype=int)

    fig, (ax1, ax_rsi) = plt.subplots(
        2, 1, figsize=(16, 9), facecolor="#0d0d0d",
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.04}
    )
    ax2 = ax1.twinx()
    for ax in [ax1, ax2, ax_rsi]:
        ax.set_facecolor("#0d0d0d")
    x = np.arange(len(closes))

    ax2.fill_between(x, buys,  alpha=0.18, color="#00c853")
    ax2.fill_between(x, sells, alpha=0.18, color="#ff1744")
    ax2.plot(x, buys,  color="#00c853", linewidth=0.9, alpha=0.8)
    ax2.plot(x, sells, color="#ff1744", linewidth=0.9, alpha=0.8)

    for lvl in levels:
        ax1.axhline(lvl, color="#3d7fff", linewidth=0.6, linestyle="--", alpha=0.5)

    trend_avg = np.mean(closes[-TREND_PERIOD:])
    ax1.axhline(trend_avg, color="#ff4444", linewidth=1.6, linestyle="-", alpha=0.9, zorder=5)

    ax1.plot(x, closes, color="#e0e0e0", linewidth=1.4, zorder=6)
    ax1.scatter(x, closes, color="#111111", s=12, zorder=7, linewidths=0.5, edgecolors="#e0e0e0")

    if len(extrema_idx) > 0:
        ax1.scatter(extrema_idx, closes[extrema_idx], color="#3d7fff", s=30, zorder=8, linewidths=0)

    # RSI панель
    ax_rsi.axhline(70, color="#ff4444", linewidth=0.7, linestyle="--", alpha=0.6)
    ax_rsi.axhline(30, color="#00c853", linewidth=0.7, linestyle="--", alpha=0.6)
    ax_rsi.axhline(50, color="#555555", linewidth=0.5, linestyle="--", alpha=0.5)
    ax_rsi.fill_between(x, rsi_series, 50, where=(rsi_series >= 50),
                        alpha=0.25, color="#00c853", interpolate=True)
    ax_rsi.fill_between(x, rsi_series, 50, where=(rsi_series < 50),
                        alpha=0.25, color="#ff1744", interpolate=True)
    ax_rsi.plot(x, rsi_series, color="#c0c0c0", linewidth=1.2)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_yticks([30, 50, 70])
    ax_rsi.text(len(x) - 1, rsi_val, f" {rsi_val:.0f}", color="#ffffff",
                fontsize=8, va="center", fontfamily="monospace")
    ax_rsi.set_ylabel("RSI", color="#888888", fontsize=8)

    step = max(1, len(times) // 10)
    ax1.set_xticks([])
    ax_rsi.set_xticks(x[::step])
    ax_rsi.set_xticklabels([t.strftime("%H:%M") for t in times[::step]], color="#888888", fontsize=8)

    for ax in [ax1, ax2, ax_rsi]:
        ax.tick_params(colors="#888888", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#333333")
        ax.grid(False)

    price_now = closes[-1]
    chg_24h   = float(ticker.get("priceChangePercent", 0))
    vol_24h   = float(ticker.get("quoteVolume", 0))
    arrow     = "▲" if price_change >= 0 else "▼"
    pair_name = format_pair_name(symbol)
    vol_str   = f"{vol_24h/1e9:.1f}B" if vol_24h >= 1e9 else f"{vol_24h/1e6:.1f}M"

    fig.text(0.01, 0.97,
             f"{pair_name}   Binance   {price_now:,.5g}   24h: {chg_24h:+.2f}%   Vol: {vol_str}   {arrow} {price_change:+.2f}%",
             color="#ffffff", fontsize=12, fontweight="bold", va="top", fontfamily="monospace")

    sig_color = "#00c853" if ("LONG" in signal_desc or "ПАМП" in signal_desc) else "#ff1744"
    fig.text(0.01, 0.91, signal_desc.replace("\n", "  |  "),
             color=sig_color, fontsize=9, va="top", fontfamily="monospace")

    window   = min(PUMP_WINDOW, len(closes) - 1)
    pump_pct = (closes[-1] - closes[-window - 1]) / closes[-window - 1] * 100
    fig.text(0.99, 0.91, f"{'🚀' if pump_pct >= 0 else '💥'} {pump_pct:+.2f}% за {window * INTERVAL_MINUTES}м",
             color="#00c853" if pump_pct >= 0 else "#ff1744",
             fontsize=9, va="top", ha="right", fontfamily="monospace")

    fig.text(0.99, 0.97, datetime.now(timezone.utc).strftime("UTC %Y-%m-%d %H:%M"),
             color="#555555", fontsize=8, va="top", ha="right", fontfamily="monospace")

    plt.tight_layout(rect=[0, 0, 1, 0.90])
    path = f"/tmp/{symbol}_signal_{int(time.time())}.png"
    plt.savefig(path, dpi=130, bbox_inches="tight", facecolor="#0d0d0d")
    plt.close(fig)
    return path

# ─── СОХРАНЕНИЕ НАСТРОЕК ──────────────────────────────────────────────────────

SETTINGS_FILE = "/root/bot_settings.json"

def load_settings():
    global MIN_VOLUME_USDT, PUMP_PCT, IIV_HOT, MIN_PAIR_AGE_DAYS
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
            MIN_VOLUME_USDT   = s.get("MIN_VOLUME_USDT",   MIN_VOLUME_USDT)
            PUMP_PCT          = s.get("PUMP_PCT",           PUMP_PCT)
            IIV_HOT           = s.get("IIV_HOT",            IIV_HOT)
            MIN_PAIR_AGE_DAYS = s.get("MIN_PAIR_AGE_DAYS",  MIN_PAIR_AGE_DAYS)
        log.info(f"Настройки загружены: vol=${MIN_VOLUME_USDT//1_000_000}M pump={PUMP_PCT}% iiv={IIV_HOT}x age={MIN_PAIR_AGE_DAYS//30}мес")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump({
            "MIN_VOLUME_USDT":   MIN_VOLUME_USDT,
            "PUMP_PCT":          PUMP_PCT,
            "IIV_HOT":           IIV_HOT,
            "MIN_PAIR_AGE_DAYS": MIN_PAIR_AGE_DAYS,
        }, f)

# ─── ГЛОБАЛЬНОЕ СОСТОЯНИЕ (для команд) ───────────────────────────────────────

g_valid_symbols: list[str] = []
g_signals_total: int = 0
g_started_at: float = time.time()
g_funding_rates: dict[str, float] = {}  # фандинг по символам
g_background_tasks: set = set()  # держим ссылки, чтобы asyncio не собрал таски раньше времени
g_reload_event: asyncio.Event = None    # сигнал перезагрузки пар

# ─── КОМАНДЫ БОТА ─────────────────────────────────────────────────────────────

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /pairs — список отслеживаемых пар."""
    if not g_valid_symbols:
        await update.message.reply_text("Бот ещё загружает пары, подожди немного.")
        return
    pairs = list(g_valid_symbols)
    text  = f"📋 <b>Отслеживаю {len(pairs)} пар:</b>\n\n"
    text += "  ".join(f"<code>{p}</code>" for p in sorted(pairs))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status — состояние бота."""
    uptime = int(time.time() - g_started_at)
    h, m   = divmod(uptime // 60, 60)
    funding_status = f"вкл (памп &gt;{FUNDING_MAX_LONG}%, дамп &lt;{FUNDING_MAX_SHORT}%)" if FUNDING_FILTER else "выкл"
    text = (
        f"🤖 <b>Статус бота</b>\n\n"
        f"Пар в слежке: <b>{len(g_valid_symbols)}</b>\n"
        f"Мин. объём: <b>${MIN_VOLUME_USDT//1_000_000}M</b>\n"
        f"Мин. возраст: <b>{MIN_PAIR_AGE_DAYS} дней</b>\n"
        f"Порог памп/дамп: <b>{PUMP_PCT}%</b>\n"
        f"IIV порог: <b>x{IIV_HOT}</b> (период {IIV_PERIOD} свечей)\n"
        f"RSI период: <b>{RSI_PERIOD}</b>\n"
        f"Фандинг-фильтр: <b>{funding_status}</b>\n"
        f"Сигналов отправлено: <b>{g_signals_total}</b>\n"
        f"Аптайм: <b>{h}ч {m}м</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Пары", callback_data="pairs"),
         InlineKeyboardButton("📊 Статус", callback_data="status")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💰 Объём: ${MIN_VOLUME_USDT//1_000_000}M", callback_data="noop"),],
        [InlineKeyboardButton("➖ 1M", callback_data="vol_down"),
         InlineKeyboardButton("➕ 1M", callback_data="vol_up")],
        [InlineKeyboardButton(f"📈 Памп/Дамп: {PUMP_PCT}%", callback_data="noop")],
        [InlineKeyboardButton("➖ 0.5%", callback_data="pump_down"),
         InlineKeyboardButton("➕ 0.5%", callback_data="pump_up")],
        [InlineKeyboardButton(f"🔥 IIV порог: x{IIV_HOT}", callback_data="noop")],
        [InlineKeyboardButton("➖ 0.5x", callback_data="iiv_down"),
         InlineKeyboardButton("➕ 0.5x", callback_data="iiv_up")],
        [InlineKeyboardButton(f"📅 Возраст пары: {MIN_PAIR_AGE_DAYS//30} мес", callback_data="noop")],
        [InlineKeyboardButton("➖ 1 мес", callback_data="age_down"),
         InlineKeyboardButton("➕ 1 мес", callback_data="age_up")],
        [InlineKeyboardButton("« Назад", callback_data="menu")],
    ])


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /menu — показать меню."""
    await update.message.reply_text(
        "Выбери действие:",
        reply_markup=main_menu_keyboard()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки меню."""
    global MIN_VOLUME_USDT, PUMP_PCT, IIV_HOT, MIN_PAIR_AGE_DAYS
    query = update.callback_query
    await query.answer()

    if query.data == "pairs":
        if not g_valid_symbols:
            await query.edit_message_text("Бот ещё загружает пары, подожди немного.")
            return
        pairs = list(g_valid_symbols)
        text  = f"📋 <b>Отслеживаю {len(pairs)} пар:</b>\n\n"
        text += "  ".join(f"<code>{p}</code>" for p in sorted(pairs))
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=main_menu_keyboard())

    elif query.data == "status":
        uptime = int(time.time() - g_started_at)
        h, m   = divmod(uptime // 60, 60)
        funding_status = f"вкл (памп &gt;{FUNDING_MAX_LONG}%, дамп &lt;{FUNDING_MAX_SHORT}%)" if FUNDING_FILTER else "выкл"
        text = (
            f"🤖 <b>Статус бота</b>\n\n"
            f"Пар в слежке: <b>{len(g_valid_symbols)}</b>\n"
            f"Мин. объём: <b>${MIN_VOLUME_USDT//1_000_000}M</b>\n"
            f"Мин. возраст: <b>{MIN_PAIR_AGE_DAYS} дней</b>\n"
            f"Порог памп/дамп: <b>{PUMP_PCT}%</b>\n"
            f"IIV порог: <b>x{IIV_HOT}</b> (период {IIV_PERIOD} свечей)\n"
            f"RSI период: <b>{RSI_PERIOD}</b>\n"
            f"Фандинг-фильтр: <b>{funding_status}</b>\n"
            f"Сигналов отправлено: <b>{g_signals_total}</b>\n"
            f"Аптайм: <b>{h}ч {m}м</b>"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=main_menu_keyboard())

    elif query.data == "settings":
        text = (
            f"⚙️ <b>Настройки бота</b>\n\n"
            f"💰 Мин. объём: <b>${MIN_VOLUME_USDT//1_000_000}M</b>\n"
            f"📈 Порог памп/дамп: <b>{PUMP_PCT}%</b>\n"
            f"🔥 IIV порог: <b>x{IIV_HOT}</b>\n"
            f"📅 Возраст пары: <b>{MIN_PAIR_AGE_DAYS//30} мес</b>\n"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=settings_keyboard())

    elif query.data == "menu":
        await query.edit_message_text("Выбери действие:", reply_markup=main_menu_keyboard())

    elif query.data == "noop":
        pass  # заголовки-кнопки — ничего не делаем

    elif query.data in ("vol_up", "vol_down", "pump_up", "pump_down", "iiv_up", "iiv_down", "age_up", "age_down"):
        if query.data == "vol_up":
            MIN_VOLUME_USDT = max(1_000_000, MIN_VOLUME_USDT + 1_000_000)
        elif query.data == "vol_down":
            MIN_VOLUME_USDT = max(1_000_000, MIN_VOLUME_USDT - 1_000_000)
        elif query.data == "pump_up":
            PUMP_PCT = round(min(20.0, PUMP_PCT + 0.5), 1)
        elif query.data == "pump_down":
            PUMP_PCT = round(max(1.0, PUMP_PCT - 0.5), 1)
        elif query.data == "iiv_up":
            IIV_HOT = round(min(20.0, IIV_HOT + 0.5), 1)
        elif query.data == "iiv_down":
            IIV_HOT = round(max(1.0, IIV_HOT - 0.5), 1)
        elif query.data == "age_up":
            MIN_PAIR_AGE_DAYS = min(36 * 30, MIN_PAIR_AGE_DAYS + 30)
        elif query.data == "age_down":
            MIN_PAIR_AGE_DAYS = max(30, MIN_PAIR_AGE_DAYS - 30)

        save_settings()
        needs_reload = query.data in ("vol_up", "vol_down", "age_up", "age_down")
        if needs_reload:
            g_reload_event.set()  # перефильтровать пары

        reload_note = "\n⏳ <i>Пары обновляются…</i>" if needs_reload else ""
        text = (
            f"⚙️ <b>Настройки бота</b>\n\n"
            f"💰 Мин. объём: <b>${MIN_VOLUME_USDT//1_000_000}M</b>\n"
            f"📈 Порог памп/дамп: <b>{PUMP_PCT}%</b>\n"
            f"🔥 IIV порог: <b>x{IIV_HOT}</b>\n"
            f"📅 Возраст пары: <b>{MIN_PAIR_AGE_DAYS//30} мес</b>"
            f"{reload_note}"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=settings_keyboard())


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

async def signal_loop(app: Application):
    """Основной цикл — получаем свечи через Websocket Binance."""
    global g_valid_symbols, g_signals_total

    bot    = app.bot
    states: dict[str, SignalState] = {}
    # Хранилище свечей для каждой пары: symbol -> list[dict]
    candles_store: dict[str, list[dict]] = {}
    # Ожидание пин-бара после памп/дамп-сигнала: symbol -> {"direction", "candles_left",
    # "pump_pct", "pump_window_min"}. direction здесь — направление ВХОДА (разворот,
    # противоположное направлению исходного памп/дампа).
    pin_bar_watches: dict[str, dict] = {}
    _last_refresh = 0.0

    async def load_symbols():
        """Загрузить и отфильтровать пары."""
        all_syms    = get_all_usdt_symbols()
        tickers     = get_24h_tickers()
        valid       = []
        for sym in all_syms:
            t = tickers.get(sym, {})
            if not is_blacklisted(sym) and has_enough_volume(t) and is_pair_old_enough(sym):
                valid.append(sym)
                if sym not in states:
                    states[sym] = SignalState()
                if sym not in candles_store:
                    # Загружаем историю свечей при старте
                    try:
                        candles_store[sym] = get_klines(sym, INTERVAL, LOOKBACK)
                    except Exception:
                        candles_store[sym] = []
            await asyncio.sleep(0.1)
        return valid, tickers

    log.info("Загружаю список пар с Binance…")
    all_symbols = get_all_usdt_symbols()
    tickers_24h = get_24h_tickers()
    log.info(f"Всего USDT-пар: {len(all_symbols)}")

    await bot.send_message(
        CHAT_ID,
        f"🤖 Бот запущен\n"
        f"Найдено пар: <b>{len(all_symbols)}</b>\n"
        f"Фильтрую по возрасту (≥ {MIN_PAIR_AGE_DAYS} дней) и объёму (≥ ${MIN_VOLUME_USDT//1_000_000}M)…\n\n"
        f"Команды: /pairs — список пар, /status — состояние",
        parse_mode=ParseMode.HTML
    )

    valid_symbols, tickers_24h = await load_symbols()
    g_valid_symbols = valid_symbols
    g_funding_rates.update(get_funding_rates())
    log.info(f"Пар после фильтра: {len(valid_symbols)}")

    await bot.send_message(
        CHAT_ID,
        f"✅ После фильтров: <b>{len(valid_symbols)} пар</b>\n"
        f"Подключаюсь к Websocket…",
        parse_mode=ParseMode.HTML
    )

    async def process_kline(symbol: str, kline: dict):
        """Обработать новую закрытую свечу."""
        if not kline.get("x"):   # x=True означает что свеча закрылась
            return

        candle = {
            "time":     datetime.fromtimestamp(kline["t"] / 1000, tz=timezone.utc),
            "open":     float(kline["o"]),
            "high":     float(kline["h"]),
            "low":      float(kline["l"]),
            "close":    float(kline["c"]),
            "vol_sell": float(kline["v"]) - float(kline["V"]),
            "vol_buy":  float(kline["V"]),
        }

        # Обновляем хранилище свечей
        if symbol not in candles_store:
            candles_store[symbol] = []
        candles_store[symbol].append(candle)
        # Держим только LOOKBACK свечей
        if len(candles_store[symbol]) > LOOKBACK:
            candles_store[symbol] = candles_store[symbol][-LOOKBACK:]

        # Если пара под наблюдением после памп/дампа — проверяем разворотный паттерн + дивергенцию RSI
        watch = pin_bar_watches.get(symbol)
        if watch:
            bearish = watch["direction"] == "short"
            # Отмена: цена ЗАКРЫЛАСЬ за экстремумом пика без разворотного паттерна — значит,
            # разворота не случилось, рывок продолжается, наблюдение больше не актуально
            invalidated = (candle["close"] > watch["extreme"]) if bearish else (candle["close"] < watch["extreme"])
            if invalidated:
                log.info(f"Наблюдение отменено (пробой пика): {symbol} — {watch['direction'].upper()}")
                del pin_bar_watches[symbol]
                watch = None

        if watch:
            watch["extreme"] = (max(watch["extreme"], candle["high"]) if bearish
                                 else min(watch["extreme"], candle["low"]))
            # Свечи от пика рывка (включительно) — окно для схлопнутого пин-бара, привязанное
            # к самому пику, а не «последние N от сейчас» (иначе к концу наблюдения окно
            # уезжает от пика и фигура, сложившаяся прямо на развороте, пропускается)
            if len(watch["since_signal"]) < PIN_BAR_COMBINE_MAX:
                watch["since_signal"].append(candle)
            pattern = find_reversal_pattern(candles_store[symbol], watch["since_signal"], bearish)
            if pattern and has_rsi_divergence(candles_store[symbol], bearish):
                entry_price = pattern["candle"]["close"]
                stop_price  = pattern["candle"]["high"] if bearish else pattern["candle"]["low"]
                pair_name   = format_pair_name(symbol)
                target_price = watch["target_price"]
                # Тейк валиден, только если он ещё впереди по ходу сделки — иначе цена уже
                # откатила дальше уровня начала движения к моменту входа
                take_price = target_price if (
                    (bearish and target_price < entry_price) or (not bearish and target_price > entry_price)
                ) else None

                # Гейт R:R — без тейка (уровень уже пройден) риск/прибыль не посчитать,
                # приравниваем к провалу фильтра. Само наблюдение снимаем в любом случае:
                # паттерн разворота уже случился, ждать другого смысла нет.
                risk   = abs(entry_price - stop_price)
                reward = abs(take_price - entry_price) if take_price is not None else None
                rr     = (reward / risk) if (reward is not None and risk > 0) else None
                if rr is None or rr < MIN_RISK_REWARD:
                    rr_str = f"{rr:.2f}" if rr is not None else "нет тейка"
                    log.info(
                        f"Вход отфильтрован по R:R ({pattern['kind']}): {symbol} — "
                        f"{watch['direction'].upper()} R:R={rr_str}"
                    )
                else:
                    entry_caption = fmt_entry_caption(
                        pair_name, watch["direction"], entry_price, stop_price, take_price,
                        pattern["kind"], candle["time"], watch["pump_pct"], watch["pump_window_min"]
                    )
                    log.info(f"Вход ({pattern['kind']}): {symbol} — {watch['direction'].upper()} {entry_price:.5g} R:R={rr:.2f}")
                    try:
                        await bot.send_message(CHAT_ID, entry_caption, parse_mode=ParseMode.HTML)
                    except Exception as e:
                        log.warning(f"Ошибка отправки входа {symbol}: {e}")
                    entry_id = save_signal(symbol, pair_name, watch["direction"], entry_price, candle["time"], pattern["kind"])
                    etask = asyncio.create_task(
                        verify_signal(symbol, watch["direction"], entry_price, None, pair_name, candle["time"], entry_id)
                    )
                    g_background_tasks.add(etask)
                    etask.add_done_callback(g_background_tasks.discard)
                del pin_bar_watches[symbol]
            else:
                watch["candles_left"] -= 1
                if watch["candles_left"] <= 0:
                    del pin_bar_watches[symbol]

        candles = candles_store[symbol]
        if len(candles) < PUMP_WINDOW + 5:
            return   # недостаточно данных

        funding   = g_funding_rates.get(symbol)
        triggered, desc, rsi_val = states[symbol].check(candles, funding)
        if not triggered:
            return

        global g_signals_total
        g_signals_total += 1

        ticker    = tickers_24h.get(symbol, {})
        chart_path = build_chart(symbol, candles, ticker, desc, rsi_val)

        price_now  = candles[-1]['close']
        window     = min(PUMP_WINDOW, len(candles) - 1)
        price_then = candles[-window - 1]['close']
        chg_24h    = float(ticker.get('priceChangePercent', 0))
        pump_pct   = (price_now - price_then) / price_then * 100
        vol_24h    = float(ticker.get('quoteVolume', 0))
        vol_str    = f"{vol_24h/1e9:.1f}B" if vol_24h >= 1e9 else f"{vol_24h/1e6:.1f}M"
        pair_name  = format_pair_name(symbol)

        signal_label = "🚀 Pump" if "ПАМП" in desc else "💥 Dump"
        direction = "long" if "ПАМП" in desc else "short"

        # Ставим пару под наблюдение: ждём пин-бар в сторону, обратную рывку, чтобы войти на разворот.
        # Тейк — уровень начала движения (price_then): обычно цена откатывает как минимум туда.
        # extreme — экстремум пика (high для шорт-наблюдения, low для лонг-наблюдения):
        # если цена ЗАКРОЕТСЯ за этим уровнем без разворотного паттерна — наблюдение отменяется,
        # разворота не было, идёт продолжение
        pin_bar_watches[symbol] = {
            "direction":       "short" if direction == "long" else "long",
            "candles_left":    PIN_BAR_WATCH_CANDLES,
            "pump_pct":        pump_pct,
            "pump_window_min": window * INTERVAL_MINUTES,
            "target_price":    price_then,
            "since_signal":    [candle],   # свеча пика — начало окна для схлопнутого пин-бара
            "extreme":         candle["high"] if direction == "long" else candle["low"],
        }

        caption = fmt_caption(
            pair_name, signal_label, pump_pct,
            price_then, price_now, chg_24h, vol_str, funding
        )

        log.info(f"Сигнал: {symbol} — {desc.split(chr(10))[0]}")
        try:
            with open(chart_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=CHAT_ID,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            log.warning(f"Ошибка отправки {symbol}: {e}")

        signal_time = candle["time"]
        signal_id = save_signal(symbol, pair_name, direction, price_now, signal_time, None)

        vtask = asyncio.create_task(verify_signal(symbol, direction, price_now, None, pair_name, signal_time, signal_id))
        g_background_tasks.add(vtask)
        vtask.add_done_callback(g_background_tasks.discard)

    async def verify_signal(symbol: str, direction: str, entry_price: float,
                             trend_label: str | None, pair_name: str, signal_time: datetime, signal_id: int):
        """Через SIGNAL_CHECK_MINUTES точек проверить, пошла ли цена в сторону сигнала."""
        checkpoints = []
        elapsed = 0
        for minutes in SIGNAL_CHECK_MINUTES:
            wait_s = minutes * 60 - elapsed
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            elapsed = minutes * 60

            candles = candles_store.get(symbol)
            is_stale = (
                not candles
                or datetime.now(timezone.utc) - candles[-1]["time"] > timedelta(minutes=SIGNAL_CHECK_STALE_MINUTES)
            )
            if is_stale:
                checkpoints.append((minutes, None, None))
                save_signal_check(signal_id, minutes, None, None)
                continue

            price_now = candles[-1]["close"]
            adjusted_pct, verdict = classify_signal_outcome(direction, entry_price, price_now)
            checkpoints.append((minutes, adjusted_pct, verdict))
            save_signal_check(signal_id, minutes, adjusted_pct, verdict)

        signal_time_str = signal_time.strftime("%H:%M UTC")
        lines = [f"🔍 Проверка сигнала <code>{pair_name}</code> {signal_time_str}  <i>Binance</i> (тренд был: {trend_label or '—'})"]
        for minutes, pct, verdict in checkpoints:
            if pct is None:
                lines.append(f"{minutes}м: нет свежих данных")
            else:
                lines.append(f"{minutes}м: {pct:+.2f}% — {verdict}")

        try:
            await bot.send_message(CHAT_ID, "\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Ошибка отправки проверки сигнала {symbol}: {e}")

    async def ws_listen(symbols: list[str]):
        """Подключиться к Binance Websocket и слушать свечи."""
        # Binance позволяет до 1024 стримов на соединение
        streams = "/".join(f"{s.lower()}@kline_{INTERVAL}" for s in symbols)
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"

        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    log.info(f"Websocket подключён: {len(symbols)} пар")
                    async for msg in ws:
                        data = json.loads(msg)
                        stream = data.get("stream", "")
                        sym    = stream.split("@")[0].upper()
                        kline  = data.get("data", {}).get("k", {})
                        if sym in states:
                            await process_kline(sym, kline)
            except Exception as e:
                log.warning(f"Websocket ошибка: {e} — переподключение через 5с")
                await asyncio.sleep(5)

    async def refresh_loop():
        """Обновляем тикеры, фандинг и список пар. Реагируем на смену настроек."""
        nonlocal valid_symbols, tickers_24h
        last_funding = time.time()
        last_full    = time.time()

        while True:
            # Ждём события перезагрузки или таймаут 60с
            try:
                await asyncio.wait_for(g_reload_event.wait(), timeout=60)
                g_reload_event.clear()
                log.info(f"Перезагрузка пар по запросу (vol=${MIN_VOLUME_USDT//1_000_000}M)…")
                try:
                    valid_symbols, tickers_24h = await load_symbols()
                    g_valid_symbols[:] = valid_symbols
                    await bot.send_message(
                        CHAT_ID,
                        f"♻️ Пары обновлены\n"
                        f"Фильтр объёма: <b>${MIN_VOLUME_USDT//1_000_000}M</b>\n"
                        f"Пар в слежке: <b>{len(valid_symbols)}</b>",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    log.warning(f"Ошибка перезагрузки пар: {e}")
            except asyncio.TimeoutError:
                pass

            now = time.time()

            # Каждый час — обновляем фандинг
            if now - last_funding >= 3600:
                try:
                    g_funding_rates.update(get_funding_rates())
                    log.info("Фандинг обновлён")
                    last_funding = now
                except Exception as e:
                    log.warning(f"Ошибка обновления фандинга: {e}")

            # Каждые 12 часов — полный сброс
            if now - last_full >= 43200:
                log.info("Сброс данных каждые 12ч…")
                try:
                    valid_symbols, tickers_24h = await load_symbols()
                    g_valid_symbols[:] = valid_symbols

                    import glob
                    for f in glob.glob("/tmp/*_signal_*.png"):
                        try:
                            os.remove(f)
                        except Exception:
                            pass

                    log.info(f"Обновлено: {len(valid_symbols)} пар")
                    await bot.send_message(
                        CHAT_ID,
                        f"♻️ Сброс данных каждые 12ч\n"
                        f"Пар в слежке: <b>{len(valid_symbols)}</b>",
                        parse_mode=ParseMode.HTML
                    )
                    last_full = now
                except Exception as e:
                    log.warning(f"Ошибка обновления: {e}")

    # Запускаем websocket и refresh параллельно
    await asyncio.gather(
        ws_listen(valid_symbols),
        refresh_loop()
    )


async def main():
    global g_started_at, g_reload_event
    g_started_at  = time.time()
    g_reload_event = asyncio.Event()
    load_settings()
    init_signals_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("pairs",  cmd_pairs))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CallbackQueryHandler(callback_handler))

    async with app:
        # Устанавливаем команды — появится кнопка меню в Telegram
        await app.bot.set_my_commands([
            BotCommand("menu",     "📱 Главное меню"),
            BotCommand("pairs",    "📋 Список пар"),
            BotCommand("status",   "📊 Статус бота"),
            BotCommand("settings", "⚙️ Настройки"),
        ])
        await app.start()
        await app.updater.start_polling()
        await signal_loop(app)
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
