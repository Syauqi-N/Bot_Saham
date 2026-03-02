import logging
import math
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from tvDatafeed import Interval, TvDatafeed

from ai_router import get_ai_reply, get_backend_savior_reply, summarize_news
from linkedin_client import create_linkedin_image_post
from news_client import fetch_news

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

CACHE_TTL_SECONDS = env_int("CACHE_TTL_SECONDS", 60)
RATE_LIMIT_SECONDS = env_int("RATE_LIMIT_SECONDS", 5)
NEWS_MAX_ITEMS = max(3, min(10, env_int("NEWS_MAX_ITEMS", 5)))
BACKEND_SAVIOR_DEBUG = env_bool("BACKEND_SAVIOR_DEBUG", True)
POST_SESSION_TTL_SECONDS = env_int("POST_SESSION_TTL_SECONDS", 900)
LINKEDIN_CAPTION_MAX_CHARS = env_int("LINKEDIN_CAPTION_MAX_CHARS", 3000)

HTTP_TIMEOUT = 15

cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
rate_limit: Dict[str, float] = {}
post_drafts: Dict[str, Dict[str, Any]] = {}

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
    if re.match(r"^!explain\b", lower):
        return "explain", cleaned
    if re.match(r"^!news\b", lower):
        query = re.sub(r"^!news\s*", "", cleaned, flags=re.IGNORECASE).strip()
        return "news", normalize_news_query(query)
    if re.match(r"^!postok\b", lower):
        return "postok", None
    if re.match(r"^!cancelpost\b", lower):
        return "cancelpost", None
    if re.match(r"^!review\b", lower):
        return "review", None
    if re.match(r"^!post\b", lower):
        return "post", None

    match = re.match(r"^\$([a-z0-9\\.]+)", lower)
    if match:
        symbol = match.group(1).upper()
        if symbol.endswith(".JK"):
            symbol = symbol[:-3]
        return "quote", symbol
    return None, None


def extract_media(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    media = data.get("media")
    source = media if isinstance(media, dict) else data
    url = source.get("url") or source.get("link")
    mimetype = source.get("mimetype") or source.get("mimeType") or source.get("mediaType")
    filename = source.get("filename") or source.get("fileName")
    data_base64 = source.get("data") or source.get("base64")

    if not any([url, mimetype, filename, data_base64, data.get("hasMedia")]):
        return None
    return {
        "url": str(url).strip() if url else None,
        "mimetype": str(mimetype).strip() if mimetype else None,
        "filename": str(filename).strip() if filename else None,
        "data": str(data_base64).strip() if data_base64 else None,
    }


def extract_message(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool, Optional[Dict[str, Any]]]:
    data = payload.get("payload", payload)
    text = (
        data.get("body")
        or data.get("text")
        or data.get("message")
        or data.get("content")
    )
    chat_id = data.get("chatId") or data.get("chat_id") or data.get("from")
    from_me = bool(data.get("fromMe") or data.get("from_me"))
    media = extract_media(data)
    return text, chat_id, from_me, media


def get_post_draft(chat_id: str) -> Optional[Dict[str, Any]]:
    draft = post_drafts.get(chat_id)
    if not draft:
        return None
    updated_at = float(draft.get("updated_at", 0.0))
    if time.time() - updated_at > POST_SESSION_TTL_SECONDS:
        post_drafts.pop(chat_id, None)
        return None
    return draft


def save_post_draft(chat_id: str, draft: Dict[str, Any]) -> None:
    now = time.time()
    if "created_at" not in draft:
        draft["created_at"] = now
    draft["updated_at"] = now
    post_drafts[chat_id] = draft


def clear_post_draft(chat_id: str) -> None:
    post_drafts.pop(chat_id, None)


def missing_post_fields(draft: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    if not draft.get("image_url") and not draft.get("image_data"):
        missing.append("gambar")
    if not draft.get("caption"):
        missing.append("caption")
    return missing


def build_post_draft_progress_text(draft: Dict[str, Any], updated_parts: List[str]) -> str:
    updated = ", ".join(updated_parts)
    missing = missing_post_fields(draft)
    if missing:
        needed = ", ".join(missing)
        return (
            f"Draft LinkedIn diupdate ({updated}).\n"
            f"Yang masih kurang: {needed}.\n"
            "Kirim datanya sekarang. Kalau sudah lengkap, ketik !postok."
        )
    return (
        f"Draft LinkedIn diupdate ({updated}).\n"
        "Caption + gambar sudah lengkap. Ketik !review untuk cek draft, !postok untuk publish, atau !cancelpost buat batal."
    )


def is_image_media(media: Dict[str, Any]) -> bool:
    mimetype = str(media.get("mimetype") or "").lower()
    if mimetype:
        return mimetype.startswith("image/")
    url = str(media.get("url") or "").lower()
    return url.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))


def handle_post_mode_input(chat_id: str, text: Optional[str], media: Optional[Dict[str, Any]]) -> bool:
    draft = get_post_draft(chat_id)
    if not draft:
        return False

    updated_parts: List[str] = []
    if media:
        if not is_image_media(media):
            send_text(chat_id, "Mode !post hanya menerima file gambar (image/*).")
            return True
        image_url = media.get("url")
        image_data = media.get("data")
        if not image_url and not image_data:
            send_text(chat_id, "Gambar terdeteksi, tapi URL/data media kosong. Coba kirim ulang gambarnya.")
            return True
        draft["image_url"] = image_url
        draft["image_data"] = image_data
        draft["image_mimetype"] = media.get("mimetype") or "image/jpeg"
        updated_parts.append("gambar")

    caption = (text or "").strip()
    if caption:
        if len(caption) > LINKEDIN_CAPTION_MAX_CHARS:
            send_text(chat_id, f"Caption terlalu panjang. Maksimal {LINKEDIN_CAPTION_MAX_CHARS} karakter.")
            return True
        draft["caption"] = caption
        updated_parts.append("caption")

    if not updated_parts:
        send_text(chat_id, "Kirim gambar atau caption dulu. Pakai !cancelpost kalau mau batal.")
        return True

    save_post_draft(chat_id, draft)
    send_text(chat_id, build_post_draft_progress_text(draft, updated_parts))
    return True


def format_post_draft_review(draft: Dict[str, Any]) -> str:
    caption = str(draft.get("caption") or "").strip()
    has_image = bool(draft.get("image_url") or draft.get("image_data"))
    missing = missing_post_fields(draft)
    lines = [
        "Draft post LinkedIn:",
        f"- Gambar: {'siap' if has_image else 'belum ada'}",
    ]
    if draft.get("image_url"):
        lines.append(f"- URL gambar: {draft['image_url']}")
    if caption:
        lines.extend(["", "Caption:", caption])
    else:
        lines.extend(["", "Caption: (kosong)"])

    lines.append("")
    if missing:
        lines.append(f"Status: belum lengkap ({', '.join(missing)}).")
    else:
        lines.append("Status: siap publish.")
    lines.append("Lanjut: !postok | Batal: !cancelpost")
    return "\n".join(lines)


def handle_post_command(chat_id: str, command: str) -> str:
    if command == "post":
        save_post_draft(
            chat_id,
            {
                "caption": "",
                "image_url": None,
                "image_data": None,
                "image_mimetype": None,
            },
        )
        send_text(
            chat_id,
            "\n".join(
                [
                    "Mode post LinkedIn aktif.",
                    "Kirim gambar + caption untuk draft post.",
                    "- Boleh kirim gambar dulu, caption belakangan (atau sebaliknya).",
                    "- Review draft: !review",
                    "- Publish: !postok",
                    "- Batal: !cancelpost",
                ]
            ),
        )
        return "ok"

    if command == "cancelpost":
        if get_post_draft(chat_id):
            clear_post_draft(chat_id)
            send_text(chat_id, "Draft LinkedIn dibatalkan.")
        else:
            send_text(chat_id, "Belum ada draft !post yang aktif.")
        return "ok"

    if command == "postok":
        draft = get_post_draft(chat_id)
        if not draft:
            send_text(chat_id, "Belum ada draft. Ketik !post untuk mulai.")
            return "ok"

        missing = missing_post_fields(draft)
        if missing:
            send_text(
                chat_id,
                "Draft belum lengkap. Kurang: {missing}.\nKirim datanya dulu lalu ketik !postok lagi.".format(
                    missing=", ".join(missing)
                ),
            )
            return "ok"

        send_text(chat_id, "Sedang publish ke LinkedIn, tunggu sebentar...")
        post_id, error = create_linkedin_image_post(
            caption=str(draft.get("caption", "")).strip(),
            media_url=str(draft.get("image_url") or "").strip() or None,
            media_data_base64=str(draft.get("image_data") or "").strip() or None,
            media_mimetype=str(draft.get("image_mimetype") or "").strip() or None,
            waha_api_key=WAHA_API_KEY or None,
            waha_base_url=WAHA_BASE_URL or None,
        )
        if error:
            send_text(
                chat_id,
                "Gagal publish LinkedIn: {error}\nKamu bisa perbaiki draft lalu kirim !postok lagi, atau !cancelpost.".format(
                    error=error
                ),
            )
            return "error"

        clear_post_draft(chat_id)
        if post_id:
            send_text(chat_id, f"Post LinkedIn berhasil dipublish. Post ID: {post_id}")
        else:
            send_text(chat_id, "Post LinkedIn berhasil dipublish.")
        return "ok"

    if command == "review":
        draft = get_post_draft(chat_id)
        if not draft:
            send_text(chat_id, "Belum ada draft. Ketik !post untuk mulai.")
            return "ok"
        send_text(chat_id, format_post_draft_review(draft))
        return "ok"

    return "ignored"


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


def sanitize_debug_error(error: Optional[str], limit: int = 300) -> str:
    if not error:
        return "Tidak ada detail error."
    # Hide any accidental key-like token in error text.
    masked = re.sub(r"(sk-[A-Za-z0-9_-]{10,}|gsk_[A-Za-z0-9_-]{10,}|nvapi-[A-Za-z0-9_-]{10,})", "***", error)
    masked = re.sub(r"\s+", " ", masked).strip()
    if len(masked) > limit:
        return masked[:limit].rstrip() + "..."
    return masked


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
    
    # Calculate change if not provided
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
            "5) Backend Savior: !explain <masalah backend>",
            "6) Ringkasan berita saham: !news <topik> (contoh: !news tech)",
            "7) Berita emiten: !news goto / !news bbca",
            "8) LinkedIn auto-post: !post",
            "9) Review draft LinkedIn: !review",
            "10) Publish draft LinkedIn: !postok / batal: !cancelpost",
            "",
            "Catatan:",
            "- Data harga saham & IHSG via TradingView (tvDatafeed)",
            "- Berita dari Google News RSS",
            "- Output S/R berbasis pivot harian",
            "- AI chat umum via !ai, mentor backend via !explain, berita via !news",
            "- LinkedIn post support caption + 1 gambar",
        ]
    )


@app.route("/health", methods=["GET"])
def health() -> str:
    return "ok"


@app.route("/webhook", methods=["POST"])
def webhook() -> Any:
    payload = request.get_json(silent=True) or {}
    text, chat_id, from_me, media = extract_message(payload)

    if not chat_id or from_me:
        return jsonify({"status": "ignored"})

    command, symbol = parse_command(text) if text else (None, None)

    if command in {"post", "postok", "cancelpost", "review"}:
        ok, remaining = rate_limit_ok(chat_id)
        if not ok:
            send_text(chat_id, f"Mohon tunggu {remaining} detik sebelum request lagi.")
            return jsonify({"status": "rate_limited"})
        status = handle_post_command(chat_id, command)
        return jsonify({"status": status})

    if get_post_draft(chat_id) and command:
        send_text(
            chat_id,
            "Kamu masih di mode !post. Kirim caption/gambar, ketik !review untuk cek draft, !postok untuk publish, atau !cancelpost buat batal.",
        )
        return jsonify({"status": "post_mode_waiting"})

    if handle_post_mode_input(chat_id, text, media):
        return jsonify({"status": "ok"})

    if not text:
        return jsonify({"status": "ignored"})

    if not command:
        return jsonify({"status": "ignored"})

    ok, remaining = rate_limit_ok(chat_id)
    if not ok:
        send_text(chat_id, f"Mohon tunggu {remaining} detik sebelum request lagi.")
        return jsonify({"status": "rate_limited"})

    if command == "help":
        send_text(chat_id, help_text())
        return jsonify({"status": "ok"})

    if command == "ai":
        ai_text = re.sub(r"^!ai\\s*", "", text, flags=re.IGNORECASE).strip()
        if not ai_text:
            send_text(chat_id, "Ketik: !ai <teks>")
            return jsonify({"status": "ok"})
        reply, error = get_ai_reply(chat_id, ai_text)
        if error or not reply:
            send_text(chat_id, "AI lagi error. Coba lagi bentar ya.")
            return jsonify({"status": "error"})
        send_text(chat_id, reply)
        return jsonify({"status": "ok"})

    if command == "explain":
        explain_text = re.sub(r"^!explain\\s*", "", text, flags=re.IGNORECASE).strip()
        if not explain_text:
            send_text(chat_id, "Ketik: !explain <masalah backend yang mau dijelasin>")
            return jsonify({"status": "ok"})
        reply, error = get_backend_savior_reply(chat_id, explain_text)
        if error or not reply:
            if error and "BACKEND_SAVIOR_API_KEY" in error:
                send_text(chat_id, error)
            else:
                if BACKEND_SAVIOR_DEBUG:
                    detail = sanitize_debug_error(error)
                    send_text(chat_id, f"Backend Savior lagi error.\nDetail: {detail}")
                else:
                    send_text(chat_id, "Backend Savior lagi error. Coba lagi bentar ya.")
            return jsonify({"status": "error"})
        send_text(chat_id, reply)
        return jsonify({"status": "ok"})

    if command == "ihsg":
        data, error = fetch_quote(IHSG_SYMBOL, exchange="IDX")
        if error or data is None:
            send_text(chat_id, error or "Data IHSG tidak tersedia.")
            return jsonify({"status": "error"})
        message = format_quote_text(IHSG_SYMBOL, data, display="IHSG (IDX)")
        send_text(chat_id, message)
        return jsonify({"status": "ok"})

    if command == "news":
        topic = normalize_news_query(symbol)
        cache_key = f"news:{(topic or 'top').lower()}"
        cached = cache_get(cache_key)
        if cached:
            message = format_news_text(topic, str(cached.get("summary", "")), list(cached.get("articles", [])))
            send_text(chat_id, message)
            return jsonify({"status": "ok"})

        articles, error = fetch_news(topic, limit=NEWS_MAX_ITEMS)
        if error or not articles:
            send_text(chat_id, error or "Belum ada berita yang bisa ditampilkan.")
            return jsonify({"status": "error"})

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
        return jsonify({"status": "ok"})

    if command == "quote" and symbol:
        data, error = fetch_quote(symbol, exchange="IDX")
        if error or data is None:
            send_text(chat_id, error or "Data tidak tersedia.")
            return jsonify({"status": "error"})

        sr_data, sr_error = fetch_sr_levels(symbol, exchange="IDX")
        if sr_error:
            logger.warning("SR error for %s: %s", symbol, sr_error)
            sr_data = None

        message = format_quote_text(symbol, data, sr=sr_data)
        send_text(chat_id, message)
        return jsonify({"status": "ok"})

    return jsonify({"status": "ignored"})


if __name__ == "__main__":
    port = env_int("PORT", 5000)
    app.run(host="0.0.0.0", port=port)
