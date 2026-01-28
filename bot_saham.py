import logging
import math
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from tvDatafeed import Interval, TvDatafeed

load_dotenv()

app = Flask(__name__)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def env_log_level() -> int:
    value = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, value, logging.INFO)


logging.basicConfig(
    level=env_log_level(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bot_saham")

WAHA_BASE_URL = env_str("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")
WAHA_SESSION = env_str("WAHA_SESSION", "default")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")

TRADINGVIEW_USERNAME = os.getenv("TRADINGVIEW_USERNAME", "")
TRADINGVIEW_PASSWORD = os.getenv("TRADINGVIEW_PASSWORD", "")

TV_INTERVAL = env_str("TV_INTERVAL", "1d")
TV_BARS = env_int("TV_BARS", 2)
IHSG_SYMBOL = env_str("IHSG_SYMBOL", "COMPOSITE").upper()
SR_INTERVAL = Interval.in_daily
SR_BARS = 3

CACHE_TTL_SECONDS = env_int("CACHE_TTL_SECONDS", 15)
RATE_LIMIT_SECONDS = env_int("RATE_LIMIT_SECONDS", 5)

HTTP_TIMEOUT = 15

INTERVAL_MAP = {
    "1m": Interval.in_1_minute,
    "5m": Interval.in_5_minute,
    "15m": Interval.in_15_minute,
    "1h": Interval.in_1_hour,
    "1d": Interval.in_daily,
}

tv_client: Optional[TvDatafeed] = None
tv_client_error: Optional[str] = None

cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
rate_limit: Dict[str, float] = {}

http_session = requests.Session()


def get_tv_client() -> Optional[TvDatafeed]:
    global tv_client, tv_client_error
    if tv_client is not None:
        return tv_client
    if tv_client_error:
        return None
    try:
        if TRADINGVIEW_USERNAME and TRADINGVIEW_PASSWORD:
            tv_client = TvDatafeed(TRADINGVIEW_USERNAME, TRADINGVIEW_PASSWORD)
        else:
            tv_client = TvDatafeed()
        return tv_client
    except Exception as exc:
        tv_client_error = str(exc)
        logger.exception("Failed to initialize TvDatafeed: %s", exc)
        return None


def safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def format_number(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def format_time_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                return value
        return str(value)
    except Exception:
        return str(value)


def format_change(change: Optional[float], pct: Optional[float]) -> str:
    if change is None or pct is None:
        return "-"
    sign = "+" if change >= 0 else ""
    return f"{sign}{format_number(change)} ({sign}{pct:.2f}%)"


def cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = cache.get(key)
    if not entry:
        return None
    cached_at, data = entry
    if time.time() - cached_at > CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return data


def cache_set(key: str, value: Dict[str, Any]) -> None:
    cache[key] = (time.time(), value)


def parse_command(text: str) -> Tuple[Optional[str], Optional[str]]:
    cleaned = text.strip()
    lower = cleaned.lower()
    if lower.startswith("!help"):
        return "help", None
    if re.match(r"^!ihsg\b", lower):
        return "ihsg", None
    match = re.match(r"^\$([a-z0-9\\.]+)", lower)
    if match:
        symbol = match.group(1).upper()
        if symbol.endswith(".JK"):
            symbol = symbol[:-3]
        return "quote", symbol
    return None, None


def extract_message(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool]:
    data = payload.get("payload", payload)
    text = (
        data.get("body")
        or data.get("text")
        or data.get("message")
        or data.get("content")
    )
    chat_id = data.get("chatId") or data.get("chat_id") or data.get("from")
    from_me = bool(data.get("fromMe") or data.get("from_me"))
    return text, chat_id, from_me


def rate_limit_ok(chat_id: str) -> Tuple[bool, int]:
    now = time.time()
    last = rate_limit.get(chat_id)
    if last and (now - last) < RATE_LIMIT_SECONDS:
        remaining = int(RATE_LIMIT_SECONDS - (now - last))
        return False, max(1, remaining)
    rate_limit[chat_id] = now
    return True, 0


def send_text(chat_id: str, text: str) -> None:
    footer = "© Haris Stockbit"
    if text and not text.rstrip().endswith(footer):
        text = f"{text.rstrip()}\n\n{footer}"
    url = f"{WAHA_BASE_URL}/api/sendText"
    payload = {"chatId": chat_id, "text": text, "session": WAHA_SESSION}
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-API-Key"] = WAHA_API_KEY
        headers["Authorization"] = f"Bearer {WAHA_API_KEY}"
    try:
        response = http_session.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        if response.status_code >= 400:
            logger.error("WAHA sendText failed: %s %s", response.status_code, response.text)
    except Exception as exc:
        logger.exception("WAHA sendText error: %s", exc)


def fetch_quote(symbol: str, exchange: str = "IDX") -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    interval = INTERVAL_MAP.get(TV_INTERVAL, Interval.in_daily)
    cache_key = f"{exchange}:{symbol}:{TV_INTERVAL}"
    cached = cache_get(cache_key)
    if cached:
        return cached, None
    tv = get_tv_client()
    if tv is None:
        return None, "Gagal login ke TradingView. Periksa kredensial."
    try:
        bars = tv.get_hist(symbol=symbol, exchange=exchange, interval=interval, n_bars=TV_BARS)
    except Exception as exc:
        logger.exception("tvDatafeed error: %s", exc)
        return None, "Gagal mengambil data. Coba lagi nanti."
    if bars is None or bars.empty:
        return None, "Data tidak tersedia untuk simbol tersebut."
    last = bars.iloc[-1]
    prev = bars.iloc[-2] if len(bars) > 1 else None
    last_close = safe_float(last.get("close"))
    prev_close = safe_float(prev.get("close")) if prev is not None else None
    data = {
        "open": safe_float(last.get("open")),
        "high": safe_float(last.get("high")),
        "low": safe_float(last.get("low")),
        "close": last_close,
        "volume": safe_float(last.get("volume")),
        "timestamp": str(last.name) if last is not None else None,
        "prev_close": prev_close,
    }
    cache_set(cache_key, data)
    return data, None


def fetch_sr_levels(symbol: str, exchange: str = "IDX") -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    cache_key = f"{exchange}:{symbol}:sr:1d:{SR_BARS}"
    cached = cache_get(cache_key)
    if cached:
        return cached, None
    tv = get_tv_client()
    if tv is None:
        return None, "Gagal login ke TradingView."
    try:
        bars = tv.get_hist(symbol=symbol, exchange=exchange, interval=SR_INTERVAL, n_bars=SR_BARS)
    except Exception as exc:
        logger.exception("tvDatafeed SR error: %s", exc)
        return None, "Gagal mengambil data SR."
    if bars is None or bars.empty:
        return None, "Data SR tidak tersedia."

    idx = -2 if len(bars) > 1 else -1
    bar = bars.iloc[idx]
    high = safe_float(bar.get("high"))
    low = safe_float(bar.get("low"))
    close = safe_float(bar.get("close"))
    bar_time = format_time_value(bars.index[idx]) if len(bars.index) else None

    if high is None or low is None or close is None or high == low:
        return None, "Data SR tidak valid."

    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = high + 2 * (pivot - low)
    s3 = low - 2 * (high - pivot)

    data = {
        "s1": s1,
        "s2": s2,
        "s3": s3,
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "time": bar_time,
    }
    cache_set(cache_key, data)
    return data, None


def format_quote_text(
    symbol: str,
    exchange: str,
    data: Dict[str, Any],
    display: Optional[str] = None,
    sr: Optional[Dict[str, Any]] = None,
) -> str:
    close = data.get("close")
    open_price = data.get("open")
    change = None if close is None or open_price is None else close - open_price
    pct = None if close is None or open_price in (None, 0) else (change / open_price) * 100
    header = display if display else f"{symbol} ({exchange})"
    lines = [
        header,
        f"Close: {format_number(close)}",
        f"Change: {format_change(change, pct)}",
        f"O/H/L: {format_number(data.get('open'))} / {format_number(data.get('high'))} / {format_number(data.get('low'))}",
        f"Volume: {format_number(data.get('volume'))}",
    ]
    if data.get("timestamp"):
        lines.append(f"Time: {data.get('timestamp')}")
    if sr:
        lines.append("")
        lines.extend(
            [
                "📊 SUPPORT & RESISTANCE — {symbol} (1 Day)".format(symbol=symbol),
                "",
                "🔻 Support",
                "S1: {s1}".format(s1=format_number(sr.get("s1"))),
                "S2: {s2}".format(s2=format_number(sr.get("s2"))),
                "S3: {s3}".format(s3=format_number(sr.get("s3"))),
                "",
                "🔺 Resistance",
                "R1: {r1}".format(r1=format_number(sr.get("r1"))),
                "R2: {r2}".format(r2=format_number(sr.get("r2"))),
                "R3: {r3}".format(r3=format_number(sr.get("r3"))),
                "",
                "⏱ {time}".format(time=sr.get("time") or "-"),
                "© Haris Stockbit",
            ]
        )
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join(
        [
            "Panduan cepat:",
            "1) Kirim kode saham dengan format: $KODE (contoh: $BBCA)",
            "2) Lihat IHSG: !ihsg",
            "3) Lihat bantuan: !help",
            "",
            "Catatan:",
            "- Data TradingView timeframe 1D",
            "- Output S/R berbasis pivot harian",
        ]
    )


@app.route("/health", methods=["GET"])
def health() -> str:
    return "ok"


@app.route("/webhook", methods=["POST"])
def webhook() -> Any:
    payload = request.get_json(silent=True) or {}
    text, chat_id, from_me = extract_message(payload)

    if not text or not chat_id or from_me:
        return jsonify({"status": "ignored"})

    command, symbol = parse_command(text)
    if not command:
        return jsonify({"status": "ignored"})

    ok, remaining = rate_limit_ok(chat_id)
    if not ok:
        send_text(chat_id, f"Mohon tunggu {remaining} detik sebelum request lagi.")
        return jsonify({"status": "rate_limited"})

    if command == "help":
        send_text(chat_id, help_text())
        return jsonify({"status": "ok"})

    if command == "ihsg":
        data, error = fetch_quote(IHSG_SYMBOL, exchange="IDX")
        if error or data is None:
            send_text(chat_id, error or "Data IHSG tidak tersedia.")
            return jsonify({"status": "error"})
        message = format_quote_text(IHSG_SYMBOL, "IDX", data, display="IHSG (IDX)")
        send_text(chat_id, message)
        return jsonify({"status": "ok"})

    if command == "quote" and symbol:
        data, error = fetch_quote(symbol, exchange="IDX")
        if error or data is None:
            send_text(chat_id, error or "Data tidak tersedia.")
            return jsonify({"status": "error"})
        sr_data = None
        sr_data, sr_error = fetch_sr_levels(symbol, exchange="IDX")
        if sr_error:
            logger.warning("SR error for %s: %s", symbol, sr_error)
            sr_data = None
        message = format_quote_text(symbol, "IDX", data, sr=sr_data)
        send_text(chat_id, message)
        return jsonify({"status": "ok"})

    return jsonify({"status": "ignored"})


if __name__ == "__main__":
    port = env_int("PORT", 5000)
    app.run(host="0.0.0.0", port=port)
