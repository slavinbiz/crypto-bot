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

import ema_trend
import ema_pullback

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

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = "8892073473:AAFe1oVfpGXRTh_SHEoL7UD_OsYvTyho2SA"   # вставь токен от @BotFather
CHAT_ID        = "-1003708330324"      # вставь chat_id куда слать сигналы

INTERVAL       = "1m"                     # 1-минутные свечи
LOOKBACK       = 120                      # сколько свечей на графике (2 часа)
CHECK_EVERY    = 60                       # проверка каждые 60 секунд
PAIR_DELAY     = 0.5                      # пауза между парами (сек)

# Параметры сигналов
TREND_PERIOD       = 60        # период скользящей средней (тренд)
VOLUME_RATIO_MIN   = 2.0       # дисбаланс buy/sell для сигнала
PRICE_CHANGE_PCT   = 0.3       # % пробоя тренда

# Памп/Дамп
PUMP_PCT           = 6.0       # % изменения цены для сигнала
PUMP_WINDOW        = 60        # за сколько минут считать памп

# Проверка сигналов постфактум — отследить, пошла ли цена в сторону сигнала
SIGNAL_CHECK_MINUTES       = [15, 60, 240]   # через сколько минут проверять цену
SIGNAL_CHECK_DEADZONE_PCT  = 0.5             # % — в пределах этого считаем "около входа"
SIGNAL_CHECK_STALE_MINUTES = 5               # свеча старше этого — считаем данные протухшими

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


def save_signal(symbol: str, pair_name: str, direction: str, entry_price: float,
                 signal_time: datetime, trend_label: str | None,
                 db_path: str = SIGNALS_DB) -> int:
    """Записать новый сигнал, вернуть его id."""
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


def fetch_trend_verdict(symbol: str, direction: str, price: float) -> dict:
    """Обёртка над ema_trend.get_trend_verdict с сетевыми запросами и обработкой ошибок."""
    try:
        trend_candles    = get_klines(symbol, ema_trend.TREND_INTERVAL, ema_trend.TREND_LIMIT, timeout=4)
        pullback_candles = get_klines(symbol, ema_trend.PULLBACK_INTERVAL, ema_trend.PULLBACK_LIMIT, timeout=4)
        return ema_trend.get_trend_verdict(direction, price, trend_candles, pullback_candles)
    except Exception as e:
        log.warning(f"Не удалось получить тренд-вердикт для {symbol}: {e}")
        return {"verdict": "unknown", "label": "⚪ Не удалось проверить", "distance_pct": None}


def fetch_pullback_signal(symbol: str, direction: str, price: float) -> dict | None:
    """Обёртка над ema_pullback.build_pullback_signal с сетевым запросом недельных свечей."""
    try:
        weekly_candles = get_klines(symbol, ema_pullback.WEEKLY_INTERVAL, ema_pullback.WEEKLY_LIMIT, timeout=4)
        return ema_pullback.build_pullback_signal(direction, price, weekly_candles)
    except Exception as e:
        log.warning(f"Не удалось получить контр-сигнал для {symbol}: {e}")
        return None


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


def fmt_pullback_caption(pair_name: str, pullback: dict) -> str:
    direction_label = "🟢 LONG" if pullback["direction"] == "long" else "🔴 SHORT"
    lines = [
        f"🎯 Контр-сигнал <code>{pair_name}</code>  <i>Binance</i>  недельный EMA | {direction_label}",
        "",
        f"ВХОД: <code>{pullback['entry']:.5g}</code> (лимитка, EMA{pullback['entry_period']}(W))",
        f"СТОП: <code>{pullback['stop']:.5g}</code>",
        f"ТЕЙК: <code>{pullback['take']:.5g}</code>",
        "",
        "Уровни:",
    ]
    for period in sorted(pullback["emas"]):
        lines.append(f"• EMA{period}(W): <code>{pullback['emas'][period]:.5g}</code>")
    return "\n".join(lines)


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
                f"Рост +{pump_pct:.2f}% за {window} мин\n"
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
                f"Падение {pump_pct:.2f}% за {window} мин\n"
                f"{price_ago:,.5g} → {price:,.5g}\n"
                f"IIV x{iiv:.1f} (порог {IIV_HOT}x){rsi_label}"
            )
            self.last_signal_time  = time.time()
            self.last_signal_price = price
            return True, desc, rsi

        return False, "", rsi

# ─── ГРАФИК ───────────────────────────────────────────────────────────────────

def build_chart(symbol: str, candles: list[dict], ticker: dict, signal_desc: str, rsi_val: float = 50.0) -> str:
    times  = [c["time"] for c in candles]
    closes = np.array([c["close"] for c in candles])
    buys   = np.array([c["vol_buy"] for c in candles])
    sells  = np.array([c["vol_sell"] for c in candles])

    # RSI для каждой свечи (скользящий)
    rsi_series = np.full(len(closes), np.nan)
    for i in range(RSI_PERIOD, len(closes)):
        rsi_series[i] = calc_rsi(closes[:i + 1])

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
    pair_name = symbol.replace("USDT", "/USDT")
    vol_str   = f"{vol_24h/1e9:.1f}B" if vol_24h >= 1e9 else f"{vol_24h/1e6:.1f}M"

    fig.text(0.01, 0.97,
             f"{pair_name}   Binance   {price_now:,.5g}   24h: {chg_24h:+.2f}%   Vol: {vol_str}   {arrow} {price_change:+.2f}%",
             color="#ffffff", fontsize=12, fontweight="bold", va="top", fontfamily="monospace")

    sig_color = "#00c853" if ("LONG" in signal_desc or "ПАМП" in signal_desc) else "#ff1744"
    fig.text(0.01, 0.91, signal_desc.replace("\n", "  |  "),
             color=sig_color, fontsize=9, va="top", fontfamily="monospace")

    window   = min(PUMP_WINDOW, len(closes) - 1)
    pump_pct = (closes[-1] - closes[-window - 1]) / closes[-window - 1] * 100
    fig.text(0.99, 0.91, f"{'🚀' if pump_pct >= 0 else '💥'} {pump_pct:+.2f}% за {window}м",
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
            ema_trend.EMA_DISTANCE_THRESHOLD_PCT = s.get(
                "EMA_DISTANCE_THRESHOLD_PCT", ema_trend.EMA_DISTANCE_THRESHOLD_PCT
            )
        log.info(f"Настройки загружены: vol=${MIN_VOLUME_USDT//1_000_000}M pump={PUMP_PCT}% iiv={IIV_HOT}x age={MIN_PAIR_AGE_DAYS//30}мес ema_dist={ema_trend.EMA_DISTANCE_THRESHOLD_PCT}%")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump({
            "MIN_VOLUME_USDT":   MIN_VOLUME_USDT,
            "PUMP_PCT":          PUMP_PCT,
            "IIV_HOT":           IIV_HOT,
            "MIN_PAIR_AGE_DAYS": MIN_PAIR_AGE_DAYS,
            "EMA_DISTANCE_THRESHOLD_PCT": ema_trend.EMA_DISTANCE_THRESHOLD_PCT,
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
    pairs = [s.replace("USDT", "/USDT") for s in g_valid_symbols]
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
        pairs = [s.replace("USDT", "/USDT") for s in g_valid_symbols]
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
        pair_name  = symbol.replace("USDT", "/USDT")

        signal_label = "🚀 Pump" if "ПАМП" in desc else "💥 Dump"
        direction = "long" if "ПАМП" in desc else "short"

        trend_verdict = await asyncio.to_thread(fetch_trend_verdict, symbol, direction, price_now)

        caption = fmt_caption(
            pair_name, signal_label, pump_pct,
            price_then, price_now, chg_24h, vol_str, funding,
            trend_label=trend_verdict["label"]
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
        signal_id = save_signal(symbol, pair_name, direction, price_now, signal_time, trend_verdict["label"])

        vtask = asyncio.create_task(verify_signal(symbol, direction, price_now, trend_verdict["label"], pair_name, signal_time, signal_id))
        g_background_tasks.add(vtask)
        vtask.add_done_callback(g_background_tasks.discard)

        ptask = asyncio.create_task(send_pullback_signal(symbol, direction, price_now, pair_name))
        g_background_tasks.add(ptask)
        ptask.add_done_callback(g_background_tasks.discard)

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
        streams = "/".join(f"{s.lower()}@kline_1m" for s in symbols)
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
