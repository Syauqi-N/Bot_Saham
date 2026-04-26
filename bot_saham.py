import base64
import logging
import math
import mimetypes
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from tvDatafeed import Interval, TvDatafeed

from ai_router import get_ai_reply, summarize_news
from news_client import fetch_news

load_dotenv()


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


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


logging.basicConfig(
    level=env_log_level(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bot_saham")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_BASE_URL = env_str("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/")
TELEGRAM_POLL_TIMEOUT_SECONDS = max(1, env_int("TELEGRAM_POLL_TIMEOUT_SECONDS", 30))
TELEGRAM_DROP_PENDING_UPDATES = env_bool("TELEGRAM_DROP_PENDING_UPDATES", True)

TRADINGVIEW_USERNAME = os.getenv("TRADINGVIEW_USERNAME", "")
TRADINGVIEW_PASSWORD = os.getenv("TRADINGVIEW_PASSWORD", "")
TV_INTERVAL = env_str("TV_INTERVAL", "1d")
TV_BARS = env_int("TV_BARS", 2)
IHSG_SYMBOL = env_str("IHSG_SYMBOL", "COMPOSITE").upper()
SR_INTERVAL = Interval.in_daily
SR_BARS = 3

CACHE_TTL_SECONDS = env_int("CACHE_TTL_SECONDS", 60)
RATE_LIMIT_SECONDS = env_int("RATE_LIMIT_SECONDS", 5)
NEWS_MAX_ITEMS = max(3, min(10, env_int("NEWS_MAX_ITEMS", 5)))

HTTP_TIMEOUT = 15
HTTP_CONNECT_TIMEOUT = 10
POLL_RETRY_DELAY_SECONDS = 3

cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
rate_limit: Dict[str, float] = {}

INTERVAL_MAP = {
    "1m": Interval.in_1_minute,
    "5m": Interval.in_5_minute,
    "15m": Interval.in_15_minute,
    "1h": Interval.in_1_hour,
    "1d": Interval.in_daily,
}

tv_client: Optional[TvDatafeed] = None
tv_client_error: Optional[str] = None

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


def format_change(change: Optional[float], pct: Optional[float]) -> str:
    if change is None or pct is None:
        return "-"
    sign = "+" if change >= 0 else ""
    return f"{sign}{format_number(change)} ({sign}{pct:.2f}%)"


def format_time_wib(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return (value + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S WIB")
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                return (parsed + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S WIB")
            except ValueError:
                return value
        return str(value)
    except Exception:
        return str(value)


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


def normalize_news_query(raw_query: Optional[str]) -> Optional[str]:
    query = (raw_query or "").strip()
    if not query:
        return None
    query = re.sub(r"\b(hari ini|today|terbaru|latest)\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\b(tentang|soal|mengenai|khusus|untuk|dong|pls|please|tolong)\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip(" ,:-")
    return query or None


def parse_command(text: str) -> Tuple[Optional[str], Optional[str]]:
    cleaned = text.strip()
    lower = cleaned.lower()
    if lower.startswith("!help"):
        return "help", None
    if re.match(r"^!ihsg\b", lower):
        return "ihsg", None
    if re.match(r"^!ai\b", lower):
        return "ai", cleaned
    if re.match(r"^!news\b", lower):
        query = re.sub(r"^!news\s*", "", cleaned, flags=re.IGNORECASE).strip()
        return "news", normalize_news_query(query)

    match = re.match(r"^\$([a-z0-9\\.]+)", lower)
    if match:
        symbol = match.group(1).upper()
        if symbol.endswith(".JK"):
            symbol = symbol[:-3]
        return "quote", symbol
    return None, None


def normalize_chat_id(chat_id: Any) -> str:
    return str(chat_id or "").strip()


def rate_limit_ok(chat_id: str) -> Tuple[bool, int]:
    now = time.time()
    last = rate_limit.get(chat_id)
    if last and (now - last) < RATE_LIMIT_SECONDS:
        remaining = int(RATE_LIMIT_SECONDS - (now - last))
        return False, max(1, remaining)
    rate_limit[chat_id] = now
    return True, 0
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
    data = {
        "open": safe_float(last.get("open")),
        "high": safe_float(last.get("high")),
        "low": safe_float(last.get("low")),
        "close": safe_float(last.get("close")),
        "volume": safe_float(last.get("volume")),
        "date": str(last.name) if last is not None else None,
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
    }
    cache_set(cache_key, data)
    return data, None


def format_quote_text(
    symbol: str,
    data: Dict[str, Any],
    display: Optional[str] = None,
    sr: Optional[Dict[str, Any]] = None,
) -> str:
    close = data.get("close")
    open_price = data.get("open")
    change = data.get("change")
    pct = data.get("pct_change")

    if change is None and close is not None and open_price is not None:
        change = close - open_price
    if pct is None and change is not None and open_price not in (None, 0):
        pct = (change / open_price) * 100

    header = display if display else f"{symbol} (IDX)"
    lines = [
        header,
        f"Close: {format_number(close)}",
        f"Change: {format_change(change, pct)}",
        f"O/H/L: {format_number(data.get('open'))} / {format_number(data.get('high'))} / {format_number(data.get('low'))}",
        f"Volume: {format_number(data.get('volume'))}",
    ]

    if data.get("date"):
        lines.append(f"Time: {format_time_wib(data.get('date'))}")

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
            ]
        )
    return "\n".join(lines)


def fallback_news_summary(articles: List[Dict[str, Any]]) -> str:
    lines = ["Ringkasan cepat (fallback):"]
    for article in articles[:3]:
        title = article.get("title") or "(Tanpa judul)"
        lines.append(f"- {title}")
    lines.append("AI summary lagi error, ini headline utama dulu.")
    return "\n".join(lines)


def format_news_text(topic: Optional[str], summary: str, articles: List[Dict[str, Any]]) -> str:
    title = f"📰 Berita: {topic}" if topic else "📰 Berita Hari Ini"
    lines = [title, ""]
    if summary.strip():
        lines.append(summary.strip())
        lines.append("")

    lines.append("Sumber utama:")
    for index, article in enumerate(articles, start=1):
        article_title = article.get("title") or "(Tanpa judul)"
        source = article.get("source")
        published = article.get("published")
        meta_parts = [part for part in [source, published] if part]
        meta = f" ({' | '.join(meta_parts)})" if meta_parts else ""
        lines.append(f"{index}. {article_title}{meta}")
        if article.get("link"):
            lines.append(str(article["link"]))
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join(
        [
            "Panduan cepat:",
            "1) Kirim kode saham dengan format: $KODE (contoh: $BBCA)",
            "2) Lihat IHSG: !ihsg",
            "3) Lihat bantuan: !help",
            "4) Chat AI: !ai <teks> (contoh: !ai woiii ini ihsg kenapa ancur gini)",
            "5) Ringkasan berita saham: !news <topik> (contoh: !news tech)",
            "6) Berita emiten: !news goto / !news bbca",
            "",
            "Catatan:",
            "- Bot ini dipakai lewat private chat Telegram",
            "- Data harga saham & IHSG via TradingView (tvDatafeed)",
            "- Berita dari Google News RSS",
            "- Output S/R berbasis pivot harian",
            "- AI chat umum via !ai dan berita via !news",
            "- Fokus repo publik ini: market lookup + AI assistance",
        ]
    )


def process_incoming_message(
    text: Optional[str],
    chat_id: Optional[str],
    from_me: bool,
    media: Optional[Dict[str, Any]],
    chat_type: Optional[str],
) -> str:
    if not chat_id or from_me or chat_type != "private":
        return "ignored"

    if media and media.get("error"):
        send_text(chat_id, str(media.get("error")))
        return "error"

    command, symbol = parse_command(text) if text else (None, None)

    if not text:
        return "ignored"

    if not command:
        return "ignored"

    ok, remaining = rate_limit_ok(chat_id)
    if not ok:
        send_text(chat_id, f"Mohon tunggu {remaining} detik sebelum request lagi.")
        return "rate_limited"

    if command == "help":
        send_text(chat_id, help_text())
        return "ok"

    if command == "ai":
        ai_text = re.sub(r"^!ai\s*", "", text, flags=re.IGNORECASE).strip()
        if not ai_text:
            send_text(chat_id, "Ketik: !ai <teks>")
            return "ok"
        reply, error = get_ai_reply(chat_id, ai_text)
        if error or not reply:
            send_text(chat_id, "AI lagi error. Coba lagi bentar ya.")
            return "error"
        send_text(chat_id, reply)
        return "ok"

    if command == "ihsg":
        data, error = fetch_quote(IHSG_SYMBOL, exchange="IDX")
        if error or data is None:
            send_text(chat_id, error or "Data IHSG tidak tersedia.")
            return "error"
        message = format_quote_text(IHSG_SYMBOL, data, display="IHSG (IDX)")
        send_text(chat_id, message)
        return "ok"

    if command == "news":
        topic = normalize_news_query(symbol)
        cache_key = f"news:{(topic or 'top').lower()}"
        cached = cache_get(cache_key)
        if cached:
            message = format_news_text(topic, str(cached.get("summary", "")), list(cached.get("articles", [])))
            send_text(chat_id, message)
            return "ok"

        articles, error = fetch_news(topic, limit=NEWS_MAX_ITEMS)
        if error or not articles:
            send_text(chat_id, error or "Belum ada berita yang bisa ditampilkan.")
            return "error"

        summary, summary_error = summarize_news(topic, articles)
        if summary_error or not summary:
            summary = fallback_news_summary(articles)

        cache_set(
            cache_key,
            {
                "summary": summary,
                "articles": articles,
            },
        )

        message = format_news_text(topic, summary, articles)
        send_text(chat_id, message)
        return "ok"

    if command == "quote" and symbol:
        data, error = fetch_quote(symbol, exchange="IDX")
        if error or data is None:
            send_text(chat_id, error or "Data tidak tersedia.")
            return "error"

        sr_data, sr_error = fetch_sr_levels(symbol, exchange="IDX")
        if sr_error:
            logger.warning("SR error for %s: %s", symbol, sr_error)
            sr_data = None

        message = format_quote_text(symbol, data, sr=sr_data)
        send_text(chat_id, message)
        return "ok"

    return "ignored"


def telegram_api_request(method: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Any], Optional[str]]:
    if not TELEGRAM_BOT_TOKEN:
        return None, "TELEGRAM_BOT_TOKEN belum di-set."

    url = f"{TELEGRAM_API_BASE_URL}/bot{TELEGRAM_BOT_TOKEN}/{method}"
    request_payload = payload or {}
    timeout_read = max(HTTP_TIMEOUT, TELEGRAM_POLL_TIMEOUT_SECONDS + 5) if method == "getUpdates" else HTTP_TIMEOUT
    try:
        response = http_session.post(
            url,
            json=request_payload,
            timeout=(HTTP_CONNECT_TIMEOUT, timeout_read),
        )
    except requests.exceptions.RequestException as exc:
        return None, f"Telegram {method} request error: {exc}"

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code >= 400 or not body.get("ok", False):
        description = body.get("description") or response.text[:300]
        return None, f"Telegram {method} error {response.status_code}: {description}"
    return body.get("result"), None


def build_telegram_file_url(file_path: str) -> str:
    return f"{TELEGRAM_API_BASE_URL}/file/bot{TELEGRAM_BOT_TOKEN}/{file_path.lstrip('/')}"


def guess_mimetype(filename: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def download_telegram_media(
    file_id: str,
    filename: Optional[str],
    mimetype: Optional[str],
    message_id: Optional[int],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    file_info, error = telegram_api_request("getFile", {"file_id": file_id})
    if error or not isinstance(file_info, dict):
        return None, error or "Telegram getFile gagal."

    file_path = str(file_info.get("file_path") or "").strip()
    if not file_path:
        return None, "Telegram getFile tidak mengembalikan file_path."

    download_url = build_telegram_file_url(file_path)
    try:
        response = http_session.get(download_url, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_TIMEOUT))
    except requests.exceptions.RequestException as exc:
        return None, f"Gagal mengunduh file Telegram: {exc}"

    if response.status_code >= 400:
        return None, f"Gagal mengunduh file Telegram: HTTP {response.status_code}"

    resolved_filename = (filename or "").strip() or os.path.basename(file_path) or file_id
    resolved_mimetype = (mimetype or "").strip() or guess_mimetype(resolved_filename, "application/octet-stream")
    encoded = base64.b64encode(response.content).decode("ascii")
    return (
        {
            "url": None,
            "mimetype": resolved_mimetype,
            "filename": resolved_filename,
            "data": encoded,
            "messageId": str(message_id or file_id),
        },
        None,
    )


def extract_telegram_message(update: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool, Optional[Dict[str, Any]], Optional[str]]:
    message = update.get("message")
    if not isinstance(message, dict):
        return None, None, False, None, None

    text = message.get("text") or message.get("caption")
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = normalize_chat_id(chat.get("id"))
    chat_type = str(chat.get("type") or "").strip() or None
    from_me = bool(sender.get("is_bot"))
    message_id = message.get("message_id")

    if from_me or chat_type != "private":
        return str(text).strip() if isinstance(text, str) else None, chat_id or None, from_me, None, chat_type

    media: Optional[Dict[str, Any]] = None
    photo_items = message.get("photo")
    if isinstance(photo_items, list) and photo_items:
        largest = photo_items[-1] if isinstance(photo_items[-1], dict) else {}
        file_id = str(largest.get("file_id") or "").strip()
        if file_id:
            media, error = download_telegram_media(
                file_id=file_id,
                filename=f"photo_{message_id or file_id}.jpg",
                mimetype="image/jpeg",
                message_id=message_id if isinstance(message_id, int) else None,
            )
            if error:
                media = {"error": error}
    elif isinstance(message.get("document"), dict):
        document = message["document"]
        file_id = str(document.get("file_id") or "").strip()
        if file_id:
            media, error = download_telegram_media(
                file_id=file_id,
                filename=str(document.get("file_name") or "").strip() or None,
                mimetype=str(document.get("mime_type") or "").strip() or None,
                message_id=message_id if isinstance(message_id, int) else None,
            )
            if error:
                media = {"error": error}

    return str(text).strip() if isinstance(text, str) else None, chat_id or None, from_me, media, chat_type


def process_telegram_update(update: Dict[str, Any]) -> str:
    text, chat_id, from_me, media, chat_type = extract_telegram_message(update)
    return process_incoming_message(text, chat_id, from_me, media, chat_type)


def send_text(chat_id: str, text: str) -> None:
    footer = "© Haris Stockbit"
    if text and not text.rstrip().endswith(footer):
        text = f"{text.rstrip()}\n\n{footer}"

    result, error = telegram_api_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
        },
    )
    if error:
        logger.error("Telegram sendMessage failed for chat_id=%s: %s", chat_id, error)
        return
    logger.debug("Telegram sendMessage ok for chat_id=%s message=%s", chat_id, result)


def poll_updates_once(offset: Optional[int]) -> Optional[int]:
    payload: Dict[str, Any] = {
        "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset

    result, error = telegram_api_request("getUpdates", payload)
    if error:
        logger.error("Polling Telegram gagal: %s", error)
        time.sleep(POLL_RETRY_DELAY_SECONDS)
        return offset

    updates = result if isinstance(result, list) else []
    next_offset = offset
    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        try:
            status = process_telegram_update(update)
            logger.debug("Processed Telegram update_id=%s status=%s", update_id, status)
        except Exception as exc:
            logger.exception("Failed processing Telegram update_id=%s: %s", update_id, exc)
        if isinstance(update_id, int):
            candidate = update_id + 1
            next_offset = candidate if next_offset is None else max(next_offset, candidate)
    return next_offset


def validate_startup_config() -> None:
    missing: List[str] = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if missing:
        raise RuntimeError("Konfigurasi wajib belum di-set: " + ", ".join(missing))


def prepare_telegram_runtime() -> None:
    validate_startup_config()

    _, error = telegram_api_request(
        "deleteWebhook",
        {"drop_pending_updates": TELEGRAM_DROP_PENDING_UPDATES},
    )
    if error:
        raise RuntimeError(error)

    me, me_error = telegram_api_request("getMe")
    if me_error:
        raise RuntimeError(me_error)
    logger.info(
        "Telegram bot siap. username=@%s drop_pending_updates=%s",
        (me or {}).get("username"),
        TELEGRAM_DROP_PENDING_UPDATES,
    )


def run_bot() -> None:
    prepare_telegram_runtime()
    offset: Optional[int] = None
    while True:
        offset = poll_updates_once(offset)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("Bot dihentikan manual.")
    except Exception as exc:
        logger.exception("Bot gagal start: %s", exc)
        raise
