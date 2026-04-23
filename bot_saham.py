import base64
import json
import logging
import math
import mimetypes
import os
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from tvDatafeed import Interval, TvDatafeed

from ai_router import get_ai_reply, get_backend_savior_reply, summarize_news
from linkedin_client import create_linkedin_image_post
from mis_logbook_client import LogbookConfig, LogbookEntry, LogbookFileType, submit_logbook_entry, upload_logbook_file
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
BACKEND_SAVIOR_DEBUG = env_bool("BACKEND_SAVIOR_DEBUG", True)
POST_SESSION_TTL_SECONDS = env_int("POST_SESSION_TTL_SECONDS", 900)
LINKEDIN_CAPTION_MAX_CHARS = env_int("LINKEDIN_CAPTION_MAX_CHARS", 3000)
LINKEDIN_MAX_IMAGES = max(1, min(3, env_int("LINKEDIN_MAX_IMAGES", 3)))
LINKEDIN_ALLOWED_CHAT_IDS = {
    item.strip()
    for item in env_str("LINKEDIN_ALLOWED_CHAT_IDS", "").split(",")
    if item.strip()
}
LOGBOOK_ENABLED = env_bool("LOGBOOK_ENABLED", True)
LOGBOOK_ALLOWED_CHAT_IDS = {
    item.strip()
    for item in env_str("LOGBOOK_ALLOWED_CHAT_IDS", "").split(",")
    if item.strip()
}
LOGBOOK_CAS_LOGIN_URL = env_str(
    "LOGBOOK_CAS_LOGIN_URL",
    "https://login.pens.ac.id/cas/login?service=https%3A%2F%2Fonline.mis.pens.ac.id%2Findex.php%3FLogin%3D1%26halAwal%3D1",
)
LOGBOOK_FORM_URL = env_str("LOGBOOK_FORM_URL", "https://online.mis.pens.ac.id/mEntry_Logbook_KP1.php")
LOGBOOK_CAS_USERNAME = os.getenv("LOGBOOK_CAS_USERNAME", "")
LOGBOOK_CAS_PASSWORD = os.getenv("LOGBOOK_CAS_PASSWORD", "")
LOGBOOK_DEFAULT_START_TIME = env_str("LOGBOOK_DEFAULT_START_TIME", "08:00")
LOGBOOK_DEFAULT_END_TIME = env_str("LOGBOOK_DEFAULT_END_TIME", "17:00")
LOGBOOK_DEFAULT_RELATED = env_bool("LOGBOOK_DEFAULT_RELATED", True)
LOGBOOK_DEFAULT_COURSE_KEYWORD = env_str("LOGBOOK_DEFAULT_COURSE_KEYWORD", "RI042106")
LOGBOOK_DEFAULT_CHECKBOX = env_bool("LOGBOOK_DEFAULT_CHECKBOX", True)
LOGBOOK_TIMEOUT_CONNECT = env_int("LOGBOOK_TIMEOUT_CONNECT", 10)
LOGBOOK_TIMEOUT_READ = env_int("LOGBOOK_TIMEOUT_READ", 45)
LOGBOOK_MATERIAL_MAX_CHARS = env_int("LOGBOOK_MATERIAL_MAX_CHARS", 4000)
PORTFOLIO_API_BASE_URL = env_str("PORTFOLIO_API_BASE_URL", "").rstrip("/")
PORTFOLIO_API_SECRET = os.getenv("PORTFOLIO_API_SECRET", "").strip()
PORTFOLIO_ALLOWED_CHAT_IDS = {
    item.strip()
    for item in env_str("PORTFOLIO_ALLOWED_CHAT_IDS", "").split(",")
    if item.strip()
}

HTTP_TIMEOUT = 15
HTTP_CONNECT_TIMEOUT = 10
POLL_RETRY_DELAY_SECONDS = 3

cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
rate_limit: Dict[str, float] = {}
post_drafts: Dict[str, Dict[str, Any]] = {}
logbook_sessions: Dict[str, Dict[str, Any]] = {}
portfolio_sessions: Dict[str, Dict[str, Any]] = {}

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

if not LINKEDIN_ALLOWED_CHAT_IDS:
    logger.warning("LINKEDIN_ALLOWED_CHAT_IDS kosong. !post diizinkan untuk semua chat.")

if LOGBOOK_ENABLED:
    if not LOGBOOK_ALLOWED_CHAT_IDS:
        logger.warning("LOGBOOK_ENABLED=true tapi LOGBOOK_ALLOWED_CHAT_IDS masih kosong.")
    if not LOGBOOK_CAS_LOGIN_URL:
        logger.warning("LOGBOOK_CAS_LOGIN_URL belum di-set.")
    if not LOGBOOK_FORM_URL:
        logger.warning("LOGBOOK_FORM_URL belum di-set.")
    if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", LOGBOOK_DEFAULT_START_TIME) or not re.match(
        r"^(?:[01]\d|2[0-3]):[0-5]\d$",
        LOGBOOK_DEFAULT_END_TIME,
    ):
        logger.warning(
            "Format default jam logbook tidak valid. Start=%s End=%s",
            LOGBOOK_DEFAULT_START_TIME,
            LOGBOOK_DEFAULT_END_TIME,
        )


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
    if re.match(r"^!porto\b", lower):
        return "porto", None
    if re.match(r"^!logbook\b", lower):
        return "logbook", None
    if re.match(r"^!postok\b", lower):
        return "postok", None
    if re.match(r"^!cancelpost\b", lower):
        return "cancelpost", None
    if re.match(r"^!ok\b", lower):
        return "logbook_ok", None
    if re.match(r"^!cancel\b", lower):
        return "logbook_cancel", None
    if re.match(r"^!update\b", lower):
        return "logbook_update", None
    if re.match(r"^!skip\b", lower) or re.match(r"^!lewati\b", lower):
        return "logbook_skip", None
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


def is_valid_time_hhmm(value: str) -> bool:
    return bool(re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", str(value or "").strip()))


def today_wib_date() -> str:
    return (datetime.now(UTC) + timedelta(hours=7)).strftime("%d-%m-%Y")


def build_logbook_config() -> LogbookConfig:
    return LogbookConfig(
        cas_login_url=LOGBOOK_CAS_LOGIN_URL,
        form_url=LOGBOOK_FORM_URL,
        username=LOGBOOK_CAS_USERNAME,
        password=LOGBOOK_CAS_PASSWORD,
        timeout_connect=LOGBOOK_TIMEOUT_CONNECT,
        timeout_read=LOGBOOK_TIMEOUT_READ,
    )


def normalize_chat_id(chat_id: Any) -> str:
    return str(chat_id or "").strip()


def is_logbook_chat_allowed(chat_id: str) -> bool:
    if not LOGBOOK_ALLOWED_CHAT_IDS:
        return False
    normalized_target = normalize_chat_id(chat_id)
    for allowed in LOGBOOK_ALLOWED_CHAT_IDS:
        if normalize_chat_id(allowed) == normalized_target:
            return True
    return False


def is_linkedin_chat_allowed(chat_id: str) -> bool:
    if not LINKEDIN_ALLOWED_CHAT_IDS:
        return True
    normalized_target = normalize_chat_id(chat_id)
    for allowed in LINKEDIN_ALLOWED_CHAT_IDS:
        if normalize_chat_id(allowed) == normalized_target:
            return True
    return False


def is_portfolio_chat_allowed(chat_id: str) -> bool:
    if not PORTFOLIO_ALLOWED_CHAT_IDS:
        return False
    normalized_target = normalize_chat_id(chat_id)
    for allowed in PORTFOLIO_ALLOWED_CHAT_IDS:
        if normalize_chat_id(allowed) == normalized_target:
            return True
    return False


def decode_media_payload(media: Dict[str, Any]) -> Optional[bytes]:
    data_b64 = str(media.get("data") or "").strip()
    if not data_b64:
        return None
    normalized = data_b64.split(",", 1)[1] if "," in data_b64 else data_b64
    try:
        return base64.b64decode(normalized)
    except Exception as exc:
        logger.warning("decode_media_payload failed: %s", exc)
        return None


def new_logbook_session() -> Dict[str, Any]:
    return {
        "status": "awaiting_material",
        "date": today_wib_date(),
        "start_time": LOGBOOK_DEFAULT_START_TIME,
        "end_time": LOGBOOK_DEFAULT_END_TIME,
        "related": LOGBOOK_DEFAULT_RELATED,
        "course_keyword": LOGBOOK_DEFAULT_COURSE_KEYWORD,
        "agree_checkbox": LOGBOOK_DEFAULT_CHECKBOX,
        "material": "",
    }


def get_logbook_session(chat_id: str) -> Optional[Dict[str, Any]]:
    session = logbook_sessions.get(chat_id)
    if not session:
        return None
    updated_at = float(session.get("updated_at", 0.0))
    if updated_at and time.time() - updated_at > POST_SESSION_TTL_SECONDS:
        logbook_sessions.pop(chat_id, None)
        return None
    return session


def save_logbook_session(chat_id: str, session: Dict[str, Any]) -> None:
    now = time.time()
    if "created_at" not in session:
        session["created_at"] = now
    session["updated_at"] = now
    logbook_sessions[chat_id] = session


def clear_logbook_session(chat_id: str) -> None:
    logbook_sessions.pop(chat_id, None)


def is_logbook_command(command: Optional[str]) -> bool:
    return command in {"logbook", "logbook_ok", "logbook_cancel", "logbook_update", "logbook_skip"}


def format_logbook_draft_review(session: Dict[str, Any]) -> str:
    material = str(session.get("material") or "").strip()
    lines = [
        "Draft logbook KP:",
        f"- Tanggal: {session.get('date')}",
        f"- Jam: {session.get('start_time')} - {session.get('end_time')}",
        f"- Sesuai matkul: {'Ya' if session.get('related') else 'Tidak'}",
        f"- Matkul keyword: {session.get('course_keyword')}",
        f"- Checkbox pernyataan: {'Ya' if session.get('agree_checkbox') else 'Tidak'}",
        "",
    ]
    if material:
        lines.extend(["Kegiatan/Materi:", material, ""])
        lines.append("Ketik !ok untuk submit, !update untuk ganti materi, atau !cancel untuk batal.")
    else:
        lines.append("Kegiatan/Materi: (belum diisi)")
        lines.append("Kirim isi kegiatan/materi sekarang.")
    return "\n".join(lines)


def handle_logbook_mode_input(chat_id: str, text: Optional[str]) -> bool:
    session = get_logbook_session(chat_id)
    if not session:
        return False

    material = (text or "").strip()
    if not material:
        send_text(chat_id, "Kegiatan/materi kosong. Kirim teks kegiatan dulu.")
        return True
    if len(material) > LOGBOOK_MATERIAL_MAX_CHARS:
        send_text(chat_id, f"Kegiatan/materi terlalu panjang. Maksimal {LOGBOOK_MATERIAL_MAX_CHARS} karakter.")
        return True

    session["material"] = material
    session["status"] = "awaiting_confirmation"
    save_logbook_session(chat_id, session)
    send_text(chat_id, format_logbook_draft_review(session))
    return True


def handle_logbook_command(chat_id: str, command: str) -> str:
    if command == "logbook":
        if get_post_draft(chat_id):
            send_text(
                chat_id,
                "Mode !post masih aktif. Selesaikan dulu dengan !postok / !cancelpost sebelum pakai !logbook.",
            )
            return "post_mode_waiting"
        if not LOGBOOK_ENABLED:
            send_text(chat_id, "Fitur logbook sedang nonaktif.")
            return "ok"
        if not is_logbook_chat_allowed(chat_id):
            logger.warning("Logbook access denied for chat_id=%s", chat_id)
            send_text(chat_id, "Kamu tidak diizinkan memakai fitur logbook ini.")
            return "ok"

        session = get_logbook_session(chat_id)
        if not session:
            session = new_logbook_session()
            save_logbook_session(chat_id, session)
            logger.info("Logbook session started for chat_id=%s", chat_id)
            send_text(
                chat_id,
                "\n".join(
                    [
                        "Mode !logbook aktif.",
                        "Kirim teks kegiatan/materi harian kamu.",
                        "Setelah ringkasan muncul: !ok untuk submit, !update untuk ganti materi, !cancel untuk batal.",
                    ]
                ),
            )
            return "ok"

        send_text(chat_id, format_logbook_draft_review(session))
        return "ok"

    if command in {"logbook_ok", "logbook_cancel", "logbook_update"} and get_post_draft(chat_id):
        send_text(
            chat_id,
            "Kamu sedang di mode !post. Gunakan !postok atau !cancelpost dulu.",
        )
        return "post_mode_waiting"

    session = get_logbook_session(chat_id)
    if not session:
        send_text(chat_id, "Belum ada sesi !logbook aktif. Ketik !logbook untuk mulai.")
        return "ok"

    if command == "logbook_cancel":
        clear_logbook_session(chat_id)
        logger.info("Logbook session cancelled for chat_id=%s", chat_id)
        send_text(chat_id, "Sesi logbook dibatalkan.")
        return "ok"

    if command == "logbook_update":
        session["status"] = "awaiting_material"
        save_logbook_session(chat_id, session)
        send_text(chat_id, "Silakan kirim kegiatan/materi terbaru untuk mengganti draft.")
        return "ok"

    if command == "logbook_ok":
        material = str(session.get("material") or "").strip()
        if not material:
            send_text(chat_id, "Draft belum ada kegiatan/materi. Kirim teks kegiatan dulu.")
            return "ok"
        if not is_valid_time_hhmm(str(session.get("start_time") or "")) or not is_valid_time_hhmm(
            str(session.get("end_time") or "")
        ):
            send_text(chat_id, "Format jam default tidak valid. Periksa LOGBOOK_DEFAULT_START_TIME/END_TIME.")
            return "error"

        send_text(chat_id, "Sedang submit logbook ke MIS, tunggu sebentar...")
        entry = LogbookEntry(
            date=str(session.get("date") or today_wib_date()),
            start_time=str(session.get("start_time") or LOGBOOK_DEFAULT_START_TIME),
            end_time=str(session.get("end_time") or LOGBOOK_DEFAULT_END_TIME),
            activity=material,
            related=bool(session.get("related")),
            course_keyword=str(session.get("course_keyword") or LOGBOOK_DEFAULT_COURSE_KEYWORD),
            agree=bool(session.get("agree_checkbox")),
        )
        success, message = submit_logbook_entry(entry, build_logbook_config())
        if success:
            logger.info("Logbook submitted successfully for chat_id=%s date=%s", chat_id, entry.date)
            session["status"] = "awaiting_file"
            session["pdf_uploaded"] = False
            session["photo_uploaded"] = False
            save_logbook_session(chat_id, session)
            send_text(
                chat_id,
                "\n".join(
                    [
                        message,
                        "",
                        "📎 Unggah file opsional:",
                        "- Kirim file PDF (laporan progres, maks 1 MB)",
                        "- Kirim foto JPG/JPEG (foto kegiatan, maks 500 KB)",
                        "- Ketik !skip untuk lewati",
                    ]
                ),
            )
            return "ok"

        logger.warning("Logbook submit failed for chat_id=%s: %s", chat_id, message)
        send_text(
            chat_id,
            "Gagal submit logbook: {message}\nKamu bisa !update untuk ganti materi, !ok untuk coba lagi, atau !cancel.".format(
                message=message
            ),
        )
        return "error"

    if command == "logbook_skip":
        clear_logbook_session(chat_id)
        send_text(chat_id, "Sesi logbook selesai. File tidak diunggah.")
        return "ok"

    return "ignored"


def is_pdf_media(media: Dict[str, Any]) -> bool:
    mimetype = str(media.get("mimetype") or "").lower()
    if mimetype:
        return mimetype in ("application/pdf", "application/x-pdf")
    filename = str(media.get("filename") or "").lower()
    return filename.endswith(".pdf")


def is_jpeg_media(media: Dict[str, Any]) -> bool:
    mimetype = str(media.get("mimetype") or "").lower()
    if mimetype:
        return mimetype in ("image/jpeg", "image/jpg")
    filename = str(media.get("filename") or "").lower()
    return any(filename.endswith(ext) for ext in (".jpg", ".jpeg"))


def handle_logbook_file_upload(chat_id: str, media: Dict[str, Any]) -> bool:
    session = get_logbook_session(chat_id)
    if not session or session.get("status") != "awaiting_file":
        return False

    if is_pdf_media(media):
        file_type = LogbookFileType.PDF
        filename = media.get("filename") or "laporan.pdf"
        if not str(filename).lower().endswith(".pdf"):
            filename = "laporan.pdf"
    elif is_jpeg_media(media):
        file_type = LogbookFileType.PHOTO
        filename = media.get("filename") or "foto.jpg"
        if not any(str(filename).lower().endswith(ext) for ext in (".jpg", ".jpeg")):
            filename = "foto.jpg"
    else:
        send_text(
            chat_id,
            "Format tidak didukung. Kirim PDF (laporan) atau JPG/JPEG (foto kegiatan), atau ketik !skip.",
        )
        return True

    send_text(chat_id, "Sedang mengunggah file ke MIS, tunggu sebentar...")
    file_bytes = decode_media_payload(media)
    if not file_bytes:
        send_text(
            chat_id,
            "Gagal membaca file dari Telegram. Coba kirim ulang file-nya atau ketik !skip.",
        )
        return True

    ok, msg = upload_logbook_file(
        file_bytes=file_bytes,
        filename=str(filename),
        file_type=file_type,
        config=build_logbook_config(),
    )
    logger.info(
        "Logbook file upload chat_id=%s type=%s ok=%s msg=%s",
        chat_id,
        file_type,
        ok,
        msg,
    )

    if file_type == LogbookFileType.PDF:
        session["pdf_uploaded"] = ok
    else:
        session["photo_uploaded"] = ok

    pdf_done = session.get("pdf_uploaded", False)
    photo_done = session.get("photo_uploaded", False)

    reply_lines = [msg]
    if ok:
        if not pdf_done:
            reply_lines.append("\nMasih bisa kirim file PDF laporan, atau ketik !skip untuk selesai.")
        elif not photo_done:
            reply_lines.append("\nMasih bisa kirim foto JPG kegiatan, atau ketik !skip untuk selesai.")
        else:
            reply_lines.append("\nSemua file terunggah. Sesi logbook selesai.")
            clear_logbook_session(chat_id)
            send_text(chat_id, "\n".join(reply_lines))
            return True
    else:
        reply_lines.append("\nKirim ulang file yang benar, atau ketik !skip untuk lewati.")

    save_logbook_session(chat_id, session)
    send_text(chat_id, "\n".join(reply_lines))
    return True


def get_post_draft(chat_id: str) -> Optional[Dict[str, Any]]:
    draft = post_drafts.get(chat_id)
    if not draft:
        return None
    updated_at = float(draft.get("updated_at", 0.0))
    if time.time() - updated_at > POST_SESSION_TTL_SECONDS:
        post_drafts.pop(chat_id, None)
        return None
    return ensure_post_images_schema(draft)


def save_post_draft(chat_id: str, draft: Dict[str, Any]) -> None:
    now = time.time()
    if "created_at" not in draft:
        draft["created_at"] = now
    draft["updated_at"] = now
    post_drafts[chat_id] = draft


def clear_post_draft(chat_id: str) -> None:
    post_drafts.pop(chat_id, None)


def ensure_post_images_schema(draft: Dict[str, Any]) -> Dict[str, Any]:
    images: List[Dict[str, Optional[str]]] = []
    existing = draft.get("images")
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip() or None
            data = str(item.get("data") or "").strip() or None
            mimetype = str(item.get("mimetype") or "").strip() or "image/jpeg"
            filename = str(item.get("filename") or "").strip() or None
            if not url and not data:
                continue
            images.append({"url": url, "data": data, "mimetype": mimetype, "filename": filename})

    legacy_url = str(draft.get("image_url") or "").strip() or None
    legacy_data = str(draft.get("image_data") or "").strip() or None
    legacy_mimetype = str(draft.get("image_mimetype") or "").strip() or "image/jpeg"
    if (legacy_url or legacy_data) and len(images) < LINKEDIN_MAX_IMAGES:
        duplicated = any(item.get("url") == legacy_url and item.get("data") == legacy_data for item in images)
        if not duplicated:
            images.append({"url": legacy_url, "data": legacy_data, "mimetype": legacy_mimetype, "filename": None})

    draft["images"] = images[:LINKEDIN_MAX_IMAGES]
    draft.pop("image_url", None)
    draft.pop("image_data", None)
    draft.pop("image_mimetype", None)
    return draft


def missing_post_fields(draft: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    images = draft.get("images") or []
    if not isinstance(images, list) or len(images) == 0:
        missing.append("gambar")
    if not draft.get("caption"):
        missing.append("caption")
    return missing


def build_post_draft_progress_text(draft: Dict[str, Any], updated_parts: List[str]) -> str:
    updated = ", ".join(updated_parts)
    missing = missing_post_fields(draft)
    image_count = len(list(draft.get("images") or []))
    if missing:
        needed = ", ".join(missing)
        return (
            f"Draft LinkedIn diupdate ({updated}).\n"
            f"Gambar tersimpan: {image_count}/{LINKEDIN_MAX_IMAGES}.\n"
            f"Yang masih kurang: {needed}.\n"
            "Kirim datanya sekarang. Kalau sudah lengkap, ketik !postok."
        )
    return (
        f"Draft LinkedIn diupdate ({updated}).\n"
        f"Caption + gambar sudah lengkap ({image_count}/{LINKEDIN_MAX_IMAGES}). "
        "Ketik !review untuk cek draft, !postok untuk publish, atau !cancelpost buat batal."
    )


def is_image_media(media: Dict[str, Any]) -> bool:
    mimetype = str(media.get("mimetype") or "").lower()
    if mimetype:
        return mimetype.startswith("image/")
    filename = str(media.get("filename") or "").lower()
    return filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))


def handle_post_mode_input(chat_id: str, text: Optional[str], media: Optional[Dict[str, Any]]) -> bool:
    draft = get_post_draft(chat_id)
    if not draft:
        return False

    updated_parts: List[str] = []
    warnings: List[str] = []
    if media:
        if not is_image_media(media):
            send_text(chat_id, "Mode !post hanya menerima file gambar (image/*).")
            return True
        image_data = media.get("data")
        if not image_data:
            send_text(chat_id, "Gambar terdeteksi, tapi data medianya kosong. Coba kirim ulang gambarnya.")
            return True
        images = list(draft.get("images") or [])
        if len(images) >= LINKEDIN_MAX_IMAGES:
            warnings.append(
                f"Maksimal {LINKEDIN_MAX_IMAGES} gambar per post. "
                "Lanjut !review / !postok atau reset draft dengan !cancelpost."
            )
        else:
            images.append(
                {
                    "url": None,
                    "data": image_data,
                    "mimetype": media.get("mimetype") or "image/jpeg",
                    "filename": media.get("filename"),
                }
            )
            draft["images"] = images
            updated_parts.append(f"gambar ({len(images)}/{LINKEDIN_MAX_IMAGES})")

    caption = (text or "").strip()
    if caption:
        if len(caption) > LINKEDIN_CAPTION_MAX_CHARS:
            send_text(chat_id, f"Caption terlalu panjang. Maksimal {LINKEDIN_CAPTION_MAX_CHARS} karakter.")
            return True
        draft["caption"] = caption
        updated_parts.append("caption")

    if not updated_parts:
        if warnings:
            send_text(chat_id, "\n".join(warnings))
            return True
        send_text(chat_id, "Kirim gambar atau caption dulu. Pakai !cancelpost kalau mau batal.")
        return True

    save_post_draft(chat_id, draft)
    progress_text = build_post_draft_progress_text(draft, updated_parts)
    if warnings:
        progress_text = "\n".join(warnings) + "\n\n" + progress_text
    send_text(chat_id, progress_text)
    return True


def format_post_draft_review(draft: Dict[str, Any]) -> str:
    caption = str(draft.get("caption") or "").strip()
    images = list(draft.get("images") or [])
    image_count = len(images)
    missing = missing_post_fields(draft)
    lines = [
        "Draft post LinkedIn:",
        f"- Gambar: {image_count}/{LINKEDIN_MAX_IMAGES}",
    ]
    for index, item in enumerate(images, start=1):
        source_type = "base64" if item.get("data") else "url"
        lines.append(f"  - Gambar #{index}: siap ({source_type})")
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
        if not is_linkedin_chat_allowed(chat_id):
            logger.warning("LinkedIn post access denied for chat_id=%s", chat_id)
            send_text(chat_id, "Kamu tidak diizinkan memakai fitur LinkedIn post ini.")
            return "ok"
        save_post_draft(
            chat_id,
            {
                "caption": "",
                "images": [],
            },
        )
        send_text(
            chat_id,
            "\n".join(
                [
                    "Mode post LinkedIn aktif.",
                    f"Kirim max {LINKEDIN_MAX_IMAGES} gambar + caption untuk draft post.",
                    "- Boleh kirim gambar bertahap, caption belakangan (atau sebaliknya).",
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
            media_items=list(draft.get("images") or []),
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


def get_portfolio_session(chat_id: str) -> Optional[Dict[str, Any]]:
    session = portfolio_sessions.get(chat_id)
    if not session:
        return None
    updated_at = float(session.get("updated_at", 0.0))
    if updated_at and time.time() - updated_at > POST_SESSION_TTL_SECONDS:
        portfolio_sessions.pop(chat_id, None)
        return None
    return session


def save_portfolio_session(chat_id: str, session: Dict[str, Any]) -> None:
    now = time.time()
    if "created_at" not in session:
        session["created_at"] = now
    session["updated_at"] = now
    portfolio_sessions[chat_id] = session


def clear_portfolio_session(chat_id: str) -> None:
    portfolio_sessions.pop(chat_id, None)


def portfolio_api_request(method: str, path: str, payload: Optional[Any] = None) -> Tuple[Optional[Any], Optional[str]]:
    if not PORTFOLIO_API_BASE_URL:
        return None, "PORTFOLIO_API_BASE_URL belum di-set."
    if not PORTFOLIO_API_SECRET:
        return None, "PORTFOLIO_API_SECRET belum di-set."

    url = f"{PORTFOLIO_API_BASE_URL.rstrip('/')}{path}"
    headers = {"X-Portfolio-Secret": PORTFOLIO_API_SECRET}
    try:
        response = http_session.request(
            method.upper(),
            url,
            json=payload,
            headers=headers,
            timeout=(HTTP_CONNECT_TIMEOUT, HTTP_TIMEOUT),
        )
    except requests.exceptions.RequestException as exc:
        return None, f"Portfolio API error: {exc}"

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        return None, error or f"Portfolio API HTTP {response.status_code}"
    return body, None


def portfolio_snapshot() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    data, error = portfolio_api_request("GET", "/api/admin/portfolio")
    if error:
        return None, error
    if not isinstance(data, dict):
        return None, "Portfolio API mengembalikan data tidak valid."
    return data, None


PORTFOLIO_PROJECT_FIELDS: List[Tuple[str, str]] = [
    ("title", "Title"),
    ("summary", "Summary"),
    ("status", "Status"),
    ("featured", "Featured"),
    ("tech", "Tech Stack"),
    ("description", "Description"),
    ("repo", "Repo URL"),
    ("demo", "Demo URL"),
]

PORTFOLIO_PROFILE_FIELDS: List[Tuple[str, str]] = [
    ("name", "Name"),
    ("role", "Role"),
    ("headline", "Headline"),
    ("bio", "Bio"),
    ("location", "Location"),
    ("email", "Email"),
]

PORTFOLIO_SOCIAL_FIELDS: List[Tuple[str, str]] = [
    ("github", "GitHub URL"),
    ("linkedin", "LinkedIn URL"),
    ("whatsapp", "WhatsApp URL"),
    ("portfolio", "Portfolio URL"),
]

PORTFOLIO_ADD_PROJECT_STEPS = ["title", "slug", "summary", "status", "featured", "tech", "description", "review"]


def format_portfolio_projects(projects: List[Dict[str, Any]]) -> str:
    if not projects:
        return "Belum ada project portfolio."

    lines = ["Projects portfolio:"]
    for index, project in enumerate(projects, start=1):
        slug = project.get("slug") or "-"
        title = project.get("title") or "-"
        status = project.get("status") or "-"
        featured = " featured" if project.get("featured") else ""
        lines.append(f"{index}. {title} [{slug}] ({status}{featured})")
    return "\n".join(lines)


def parse_boolean_choice(value: str) -> Optional[bool]:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "ya"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "tidak", "enggak", "nggak"}:
        return False
    return None


def parse_project_status_choice(value: str) -> Optional[str]:
    normalized = value.strip().lower()
    mapping = {
        "1": "live",
        "2": "wip",
        "3": "private",
        "4": "archived",
        "live": "live",
        "wip": "wip",
        "private": "private",
        "archived": "archived",
    }
    return mapping.get(normalized)


def normalize_project_slug(value: str) -> Optional[str]:
    slug = value.strip().lower().replace(" ", "-")
    if re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug):
        return slug
    return None


def portfolio_menu_text() -> str:
    return "\n".join(
        [
            "Mode !porto aktif.",
            "",
            "Menu utama:",
            "1. Lihat projects",
            "2. Tambah project",
            "3. Edit project",
            "4. Hapus project",
            "5. Edit profile",
            "6. Edit social links",
            "0. Keluar",
            "",
            "Perintah umum: menu | back | save | cancel",
            "Catatan: Tech stack dan experiences tetap lewat web admin.",
        ]
    )


def send_portfolio_menu(chat_id: str, prefix: Optional[str] = None) -> None:
    text = portfolio_menu_text()
    if prefix:
        text = f"{prefix}\n\n{text}"
    send_text(chat_id, text)


def reset_portfolio_session(session: Dict[str, Any]) -> None:
    created_at = session.get("created_at")
    session.clear()
    if created_at is not None:
        session["created_at"] = created_at
    session["status"] = "active"
    session["flow"] = "menu"
    session["step"] = "menu"


def portfolio_action_from_input(value: str) -> Optional[str]:
    normalized = value.strip().lower()
    mapping = {
        "1": "list_projects",
        "list": "list_projects",
        "projects": "list_projects",
        "2": "add_project",
        "add": "add_project",
        "tambah": "add_project",
        "3": "edit_project",
        "edit": "edit_project",
        "4": "delete_project",
        "delete": "delete_project",
        "hapus": "delete_project",
        "5": "edit_profile",
        "profile": "edit_profile",
        "6": "edit_social",
        "social": "edit_social",
        "links": "edit_social",
    }
    return mapping.get(normalized)


def find_portfolio_project(snapshot: Dict[str, Any], slug: str) -> Optional[Dict[str, Any]]:
    for project in snapshot.get("projects") or []:
        if isinstance(project, dict) and str(project.get("slug") or "") == slug:
            return dict(project)
    return None


def normalize_portfolio_project(project: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slug": str(project.get("slug") or ""),
        "title": str(project.get("title") or ""),
        "summary": str(project.get("summary") or ""),
        "description": str(project.get("description") or ""),
        "status": str(project.get("status") or "wip"),
        "featured": bool(project.get("featured")),
        "techStack": list(project.get("techStack") or []),
        "repoUrl": project.get("repoUrl") or "",
        "demoUrl": project.get("demoUrl") or "",
    }


def format_portfolio_project_review(project: Dict[str, Any], title: str) -> str:
    data = normalize_portfolio_project(project)
    tech_stack = ", ".join(data.get("techStack") or []) or "-"
    return "\n".join(
        [
            title,
            f"- Slug: {data['slug']}",
            f"- Title: {data['title']}",
            f"- Summary: {data['summary']}",
            f"- Status: {data['status']}",
            f"- Featured: {'Ya' if data['featured'] else 'Tidak'}",
            f"- Tech Stack: {tech_stack}",
            f"- Repo URL: {data['repoUrl'] or '-'}",
            f"- Demo URL: {data['demoUrl'] or '-'}",
            "",
            "Description:",
            data["description"] or "-",
        ]
    )


def format_profile_review(profile: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "Draft profile:",
            f"- Name: {profile.get('name') or '-'}",
            f"- Role: {profile.get('role') or '-'}",
            f"- Headline: {profile.get('headline') or '-'}",
            f"- Location: {profile.get('location') or '-'}",
            f"- Email: {profile.get('email') or '-'}",
            "",
            "Bio:",
            str(profile.get("bio") or "-"),
        ]
    )


def format_social_review(social_links: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "Draft social links:",
            f"- GitHub: {social_links.get('githubUrl') or '-'}",
            f"- LinkedIn: {social_links.get('linkedinUrl') or '-'}",
            f"- WhatsApp: {social_links.get('telegramUrl') or '-'}",
            f"- Portfolio: {social_links.get('portfolioUrl') or '-'}",
        ]
    )


def format_field_menu(title: str, options: List[Tuple[str, str]], save_hint: str) -> str:
    lines = [title, ""]
    for index, (_, label) in enumerate(options, start=1):
        lines.append(f"{index}. {label}")
    lines.extend(["", save_hint, "Perintah: nomor field | save | back | menu | cancel"])
    return "\n".join(lines)


def resolve_field_choice(value: str, options: List[Tuple[str, str]]) -> Optional[str]:
    normalized = value.strip().lower()
    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < len(options):
            return options[index][0]
    for key, label in options:
        if normalized in {key, label.lower()}:
            return key
    return None


def format_project_selection(projects: List[Dict[str, Any]], title: str) -> str:
    return f"{title}\n\n{format_portfolio_projects(projects)}\n\nKirim nomor atau slug project. Pakai back/menu/cancel kalau mau batal."


def resolve_project_choice(value: str, projects: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    normalized = value.strip()
    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < len(projects):
            return dict(projects[index])
    normalized_slug = normalized.lower()
    for project in projects:
        if str(project.get("slug") or "").lower() == normalized_slug:
            return dict(project)
    return None


def add_project_prompt(step: str) -> str:
    prompts = {
        "title": "Tambah project.\nKirim title project.",
        "slug": "Kirim slug project.\nFormat: lowercase-hyphen, contoh `market-bot`.",
        "summary": "Kirim summary singkat project.",
        "status": "Pilih status project:\n1. live\n2. wip\n3. private\n4. archived",
        "featured": "Featured project?\nKetik: yes / no",
        "tech": "Kirim tech stack dipisah koma.\nContoh: Python, Telegram Bot API, Docker",
        "description": "Kirim description project. Boleh multi-line.",
    }
    return prompts.get(step, "Lanjutkan isi draft project.")


def update_project_field(project: Dict[str, Any], field: str, value: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    data = normalize_portfolio_project(project)
    field = field.lower().strip()
    cleaned = value.strip()

    if field == "title":
        if not cleaned:
            return None, "Title tidak boleh kosong."
        data["title"] = cleaned
    elif field == "summary":
        if not cleaned:
            return None, "Summary tidak boleh kosong."
        data["summary"] = cleaned
    elif field == "status":
        status = parse_project_status_choice(cleaned)
        if not status:
            return None, "Status tidak valid. Pilih: 1 live, 2 wip, 3 private, 4 archived."
        data["status"] = status
    elif field == "featured":
        featured = parse_boolean_choice(cleaned)
        if featured is None:
            return None, "Featured tidak valid. Pakai yes/no."
        data["featured"] = featured
    elif field == "tech":
        tech_stack = [item.strip() for item in cleaned.split(",") if item.strip()]
        if not tech_stack:
            return None, "Tech stack tidak boleh kosong."
        data["techStack"] = tech_stack
    elif field == "description":
        if not cleaned:
            return None, "Description tidak boleh kosong."
        data["description"] = cleaned
    elif field == "repo":
        data["repoUrl"] = "" if cleaned.lower() in {"-", "kosong", "clear"} else cleaned
    elif field == "demo":
        data["demoUrl"] = "" if cleaned.lower() in {"-", "kosong", "clear"} else cleaned
    else:
        return None, "Field project tidak dikenal."

    return data, None


def update_profile_field(profile: Dict[str, Any], field: str, value: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    allowed = {"name", "role", "headline", "bio", "location", "email"}
    field = field.lower().strip()
    if field not in allowed:
        return None, "Field profile tidak dikenal."
    if not value.strip():
        return None, "Nilai profile tidak boleh kosong."
    data = dict(profile)
    data[field] = value.strip()
    return data, None


def update_social_field(social_links: Dict[str, Any], field: str, value: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    field_map = {
        "github": "githubUrl",
        "linkedin": "linkedinUrl",
        "whatsapp": "telegramUrl",
        "wa": "telegramUrl",
        "portfolio": "portfolioUrl",
    }
    key = field_map.get(field.lower().strip())
    if not key:
        return None, "Field social tidak dikenal."
    if not value.strip():
        return None, "Nilai social link tidak boleh kosong."
    data = dict(social_links)
    data[key] = value.strip()
    return data, None


def prompt_project_field_value(field: str) -> str:
    prompts = {
        "title": "Kirim title baru project.",
        "summary": "Kirim summary baru project.",
        "status": "Pilih status baru:\n1. live\n2. wip\n3. private\n4. archived",
        "featured": "Featured project?\nKetik yes / no",
        "tech": "Kirim tech stack baru dipisah koma.",
        "description": "Kirim description baru project.",
        "repo": "Kirim repo URL baru. Pakai `-` untuk mengosongkan.",
        "demo": "Kirim demo URL baru. Pakai `-` untuk mengosongkan.",
    }
    return prompts.get(field, "Kirim nilai baru.")


def prompt_profile_field_value(field: str) -> str:
    labels = dict(PORTFOLIO_PROFILE_FIELDS)
    return f"Kirim nilai baru untuk {labels.get(field, field)}."


def prompt_social_field_value(field: str) -> str:
    labels = dict(PORTFOLIO_SOCIAL_FIELDS)
    return f"Kirim nilai baru untuk {labels.get(field, field)}."


def handle_portfolio_back(chat_id: str, session: Dict[str, Any]) -> str:
    flow = str(session.get("flow") or "menu")
    step = str(session.get("step") or "menu")

    if flow == "add_project":
        current_index = PORTFOLIO_ADD_PROJECT_STEPS.index(step)
        if current_index == 0:
            reset_portfolio_session(session)
            save_portfolio_session(chat_id, session)
            send_portfolio_menu(chat_id, "Kembali ke menu.")
            return "ok"
        previous_step = PORTFOLIO_ADD_PROJECT_STEPS[current_index - 1]
        session["step"] = previous_step
        save_portfolio_session(chat_id, session)
        send_text(chat_id, add_project_prompt(previous_step))
        return "ok"

    if flow == "edit_project":
        if step == "field_value":
            session["step"] = "field_menu"
            session.pop("selected_field", None)
            save_portfolio_session(chat_id, session)
            send_text(
                chat_id,
                format_portfolio_project_review(dict(session.get("draft") or {}), "Draft edit project:")
                + "\n\n"
                + format_field_menu(
                    "Pilih field project yang mau diubah:",
                    PORTFOLIO_PROJECT_FIELDS,
                    "Ketik save untuk menyimpan perubahan project.",
                ),
            )
            return "ok"
        if step == "field_menu":
            session["step"] = "select_project"
            save_portfolio_session(chat_id, session)
            send_text(chat_id, format_project_selection(list(session.get("project_choices") or []), "Pilih project yang mau diedit:"))
            return "ok"

    if flow == "delete_project" and step == "confirm":
        session["step"] = "select_project"
        save_portfolio_session(chat_id, session)
        send_text(chat_id, format_project_selection(list(session.get("project_choices") or []), "Pilih project yang mau dihapus:"))
        return "ok"

    if flow in {"edit_profile", "edit_social"}:
        if step == "field_value":
            session["step"] = "field_menu"
            session.pop("selected_field", None)
            save_portfolio_session(chat_id, session)
            if flow == "edit_profile":
                send_text(
                    chat_id,
                    format_profile_review(dict(session.get("draft") or {}))
                    + "\n\n"
                    + format_field_menu(
                        "Pilih field profile yang mau diubah:",
                        PORTFOLIO_PROFILE_FIELDS,
                        "Ketik save untuk menyimpan perubahan profile.",
                    ),
                )
            else:
                send_text(
                    chat_id,
                    format_social_review(dict(session.get("draft") or {}))
                    + "\n\n"
                    + format_field_menu(
                        "Pilih field social links yang mau diubah:",
                        PORTFOLIO_SOCIAL_FIELDS,
                        "Ketik save untuk menyimpan perubahan social links.",
                    ),
                )
            return "ok"

    reset_portfolio_session(session)
    save_portfolio_session(chat_id, session)
    send_portfolio_menu(chat_id, "Kembali ke menu.")
    return "ok"


def start_portfolio_add_project(chat_id: str, session: Dict[str, Any]) -> str:
    session["flow"] = "add_project"
    session["step"] = "title"
    session["draft"] = {
        "title": "",
        "slug": "",
        "summary": "",
        "status": "wip",
        "featured": False,
        "techStack": [],
        "description": "",
        "repoUrl": "",
        "demoUrl": "",
    }
    save_portfolio_session(chat_id, session)
    send_text(chat_id, add_project_prompt("title"))
    return "ok"


def start_portfolio_project_selection(chat_id: str, session: Dict[str, Any], flow: str, title: str) -> str:
    snapshot, error = portfolio_snapshot()
    projects = list((snapshot or {}).get("projects") or [])
    if error:
        send_text(chat_id, f"Gagal mengambil project: {error}")
        return "error"
    if not projects:
        reset_portfolio_session(session)
        save_portfolio_session(chat_id, session)
        send_portfolio_menu(chat_id, "Belum ada project di portfolio.")
        return "ok"

    session["flow"] = flow
    session["step"] = "select_project"
    session["project_choices"] = projects
    save_portfolio_session(chat_id, session)
    send_text(chat_id, format_project_selection(projects, title))
    return "ok"


def start_portfolio_edit_profile(chat_id: str, session: Dict[str, Any]) -> str:
    snapshot, error = portfolio_snapshot()
    if error or not snapshot:
        send_text(chat_id, f"Gagal mengambil profile: {error}")
        return "error"

    session["flow"] = "edit_profile"
    session["step"] = "field_menu"
    session["draft"] = dict(snapshot.get("profile") or {})
    save_portfolio_session(chat_id, session)
    send_text(
        chat_id,
        format_profile_review(dict(session.get("draft") or {}))
        + "\n\n"
        + format_field_menu(
            "Pilih field profile yang mau diubah:",
            PORTFOLIO_PROFILE_FIELDS,
            "Ketik save untuk menyimpan perubahan profile.",
        ),
    )
    return "ok"


def start_portfolio_edit_social(chat_id: str, session: Dict[str, Any]) -> str:
    snapshot, error = portfolio_snapshot()
    if error or not snapshot:
        send_text(chat_id, f"Gagal mengambil social links: {error}")
        return "error"

    session["flow"] = "edit_social"
    session["step"] = "field_menu"
    session["draft"] = dict(snapshot.get("socialLinks") or {})
    save_portfolio_session(chat_id, session)
    send_text(
        chat_id,
        format_social_review(dict(session.get("draft") or {}))
        + "\n\n"
        + format_field_menu(
            "Pilih field social links yang mau diubah:",
            PORTFOLIO_SOCIAL_FIELDS,
            "Ketik save untuk menyimpan perubahan social links.",
        ),
    )
    return "ok"


def handle_portfolio_menu(chat_id: str, session: Dict[str, Any], lower: str) -> str:
    action = portfolio_action_from_input(lower)
    if not action:
        send_portfolio_menu(chat_id, "Pilihan !porto tidak dikenal.")
        return "ok"

    if action == "list_projects":
        snapshot, error = portfolio_snapshot()
        if error or not snapshot:
            send_text(chat_id, f"Gagal mengambil portfolio: {error}")
            return "error"
        send_portfolio_menu(chat_id, format_portfolio_projects(list(snapshot.get("projects") or [])))
        return "ok"
    if action == "add_project":
        return start_portfolio_add_project(chat_id, session)
    if action == "edit_project":
        return start_portfolio_project_selection(chat_id, session, "edit_project", "Pilih project yang mau diedit:")
    if action == "delete_project":
        return start_portfolio_project_selection(chat_id, session, "delete_project", "Pilih project yang mau dihapus:")
    if action == "edit_profile":
        return start_portfolio_edit_profile(chat_id, session)
    if action == "edit_social":
        return start_portfolio_edit_social(chat_id, session)
    return "ok"


def handle_portfolio_add_project(chat_id: str, session: Dict[str, Any], user_text: str) -> str:
    draft = dict(session.get("draft") or {})
    step = str(session.get("step") or "title")
    cleaned = user_text.strip()

    if step == "title":
        if not cleaned:
            send_text(chat_id, "Title tidak boleh kosong.")
            return "ok"
        draft["title"] = cleaned
        session["step"] = "slug"
    elif step == "slug":
        slug = normalize_project_slug(cleaned)
        if not slug:
            send_text(chat_id, "Slug tidak valid. Pakai lowercase-hyphen, contoh `market-bot`.")
            return "ok"
        draft["slug"] = slug
        session["step"] = "summary"
    elif step == "summary":
        if not cleaned:
            send_text(chat_id, "Summary tidak boleh kosong.")
            return "ok"
        draft["summary"] = cleaned
        session["step"] = "status"
    elif step == "status":
        status = parse_project_status_choice(cleaned)
        if not status:
            send_text(chat_id, "Status tidak valid. Pilih: 1 live, 2 wip, 3 private, 4 archived.")
            return "ok"
        draft["status"] = status
        session["step"] = "featured"
    elif step == "featured":
        featured = parse_boolean_choice(cleaned)
        if featured is None:
            send_text(chat_id, "Featured tidak valid. Ketik yes / no.")
            return "ok"
        draft["featured"] = featured
        session["step"] = "tech"
    elif step == "tech":
        tech_stack = [item.strip() for item in cleaned.split(",") if item.strip()]
        if not tech_stack:
            send_text(chat_id, "Tech stack tidak boleh kosong.")
            return "ok"
        draft["techStack"] = tech_stack
        session["step"] = "description"
    elif step == "description":
        if not cleaned:
            send_text(chat_id, "Description tidak boleh kosong.")
            return "ok"
        draft["description"] = cleaned
        session["step"] = "review"
    elif step == "review":
        if cleaned.lower() != "save":
            send_text(chat_id, "Ketik save untuk membuat project, atau back/menu/cancel.")
            return "ok"
        _, api_error = portfolio_api_request("POST", "/api/admin/projects", draft)
        if api_error:
            send_text(chat_id, f"Gagal membuat project: {api_error}")
            return "error"
        reset_portfolio_session(session)
        save_portfolio_session(chat_id, session)
        send_portfolio_menu(chat_id, f"Project berhasil dibuat: {draft.get('slug')}")
        return "ok"

    session["draft"] = draft
    save_portfolio_session(chat_id, session)

    if session.get("step") == "review":
        send_text(
            chat_id,
            format_portfolio_project_review(draft, "Review project baru:")
            + "\n\nKetik save untuk menyimpan, back untuk revisi, atau menu/cancel.",
        )
        return "ok"

    send_text(chat_id, add_project_prompt(str(session.get("step"))))
    return "ok"


def handle_portfolio_edit_project(chat_id: str, session: Dict[str, Any], user_text: str) -> str:
    step = str(session.get("step") or "select_project")
    cleaned = user_text.strip()

    if step == "select_project":
        project = resolve_project_choice(cleaned, list(session.get("project_choices") or []))
        if not project:
            send_text(chat_id, "Project tidak ditemukan. Kirim nomor atau slug yang valid.")
            return "ok"
        session["selected_slug"] = str(project.get("slug") or "")
        session["draft"] = normalize_portfolio_project(project)
        session["step"] = "field_menu"
        save_portfolio_session(chat_id, session)
        send_text(
            chat_id,
            format_portfolio_project_review(dict(session.get("draft") or {}), "Draft edit project:")
            + "\n\n"
            + format_field_menu(
                "Pilih field project yang mau diubah:",
                PORTFOLIO_PROJECT_FIELDS,
                "Ketik save untuk menyimpan perubahan project.",
            ),
        )
        return "ok"

    if step == "field_menu":
        if cleaned.lower() == "save":
            selected_slug = str(session.get("selected_slug") or "")
            draft = dict(session.get("draft") or {})
            _, api_error = portfolio_api_request("PUT", f"/api/admin/projects/{selected_slug}", draft)
            if api_error:
                send_text(chat_id, f"Gagal update project: {api_error}")
                return "error"
            reset_portfolio_session(session)
            save_portfolio_session(chat_id, session)
            send_portfolio_menu(chat_id, f"Project berhasil diupdate: {selected_slug}")
            return "ok"

        field = resolve_field_choice(cleaned, PORTFOLIO_PROJECT_FIELDS)
        if not field:
            send_text(
                chat_id,
                format_field_menu(
                    "Pilihan field project tidak dikenal.",
                    PORTFOLIO_PROJECT_FIELDS,
                    "Ketik save untuk menyimpan perubahan project.",
                ),
            )
            return "ok"
        session["selected_field"] = field
        session["step"] = "field_value"
        save_portfolio_session(chat_id, session)
        send_text(chat_id, prompt_project_field_value(field))
        return "ok"

    field = str(session.get("selected_field") or "")
    draft = dict(session.get("draft") or {})
    payload, field_error = update_project_field(draft, field, cleaned)
    if field_error or not payload:
        send_text(chat_id, field_error or "Nilai project tidak valid.")
        return "ok"

    session["draft"] = payload
    session["step"] = "field_menu"
    session.pop("selected_field", None)
    save_portfolio_session(chat_id, session)
    send_text(
        chat_id,
        format_portfolio_project_review(payload, "Draft edit project:")
        + "\n\n"
        + format_field_menu(
            "Pilih field project yang mau diubah:",
            PORTFOLIO_PROJECT_FIELDS,
            "Ketik save untuk menyimpan perubahan project.",
        ),
    )
    return "ok"


def handle_portfolio_delete_project(chat_id: str, session: Dict[str, Any], user_text: str) -> str:
    step = str(session.get("step") or "select_project")
    cleaned = user_text.strip()

    if step == "select_project":
        project = resolve_project_choice(cleaned, list(session.get("project_choices") or []))
        if not project:
            send_text(chat_id, "Project tidak ditemukan. Kirim nomor atau slug yang valid.")
            return "ok"
        session["selected_slug"] = str(project.get("slug") or "")
        session["selected_project"] = project
        session["step"] = "confirm"
        save_portfolio_session(chat_id, session)
        send_text(
            chat_id,
            format_portfolio_project_review(project, "Project yang akan dihapus:")
            + "\n\nKetik yes untuk hapus permanen, atau back/menu/cancel.",
        )
        return "ok"

    normalized = cleaned.lower()
    if normalized not in {"yes", "ya"}:
        send_text(chat_id, "Ketik yes untuk hapus permanen, atau back/menu/cancel.")
        return "ok"

    selected_slug = str(session.get("selected_slug") or "")
    _, api_error = portfolio_api_request("DELETE", f"/api/admin/projects/{selected_slug}")
    if api_error:
        send_text(chat_id, f"Gagal hapus project: {api_error}")
        return "error"
    reset_portfolio_session(session)
    save_portfolio_session(chat_id, session)
    send_portfolio_menu(chat_id, f"Project berhasil dihapus: {selected_slug}")
    return "ok"


def handle_portfolio_edit_profile(chat_id: str, session: Dict[str, Any], user_text: str) -> str:
    step = str(session.get("step") or "field_menu")
    cleaned = user_text.strip()

    if step == "field_menu":
        if cleaned.lower() == "save":
            draft = dict(session.get("draft") or {})
            _, api_error = portfolio_api_request("PUT", "/api/admin/profile", draft)
            if api_error:
                send_text(chat_id, f"Gagal update profile: {api_error}")
                return "error"
            reset_portfolio_session(session)
            save_portfolio_session(chat_id, session)
            send_portfolio_menu(chat_id, "Profile berhasil diupdate.")
            return "ok"

        field = resolve_field_choice(cleaned, PORTFOLIO_PROFILE_FIELDS)
        if not field:
            send_text(
                chat_id,
                format_field_menu(
                    "Pilihan field profile tidak dikenal.",
                    PORTFOLIO_PROFILE_FIELDS,
                    "Ketik save untuk menyimpan perubahan profile.",
                ),
            )
            return "ok"
        session["selected_field"] = field
        session["step"] = "field_value"
        save_portfolio_session(chat_id, session)
        send_text(chat_id, prompt_profile_field_value(field))
        return "ok"

    field = str(session.get("selected_field") or "")
    draft = dict(session.get("draft") or {})
    payload, field_error = update_profile_field(draft, field, cleaned)
    if field_error or not payload:
        send_text(chat_id, field_error or "Nilai profile tidak valid.")
        return "ok"

    session["draft"] = payload
    session["step"] = "field_menu"
    session.pop("selected_field", None)
    save_portfolio_session(chat_id, session)
    send_text(
        chat_id,
        format_profile_review(payload)
        + "\n\n"
        + format_field_menu(
            "Pilih field profile yang mau diubah:",
            PORTFOLIO_PROFILE_FIELDS,
            "Ketik save untuk menyimpan perubahan profile.",
        ),
    )
    return "ok"


def handle_portfolio_edit_social(chat_id: str, session: Dict[str, Any], user_text: str) -> str:
    step = str(session.get("step") or "field_menu")
    cleaned = user_text.strip()

    if step == "field_menu":
        if cleaned.lower() == "save":
            draft = dict(session.get("draft") or {})
            _, api_error = portfolio_api_request("PUT", "/api/admin/social-links", draft)
            if api_error:
                send_text(chat_id, f"Gagal update social links: {api_error}")
                return "error"
            reset_portfolio_session(session)
            save_portfolio_session(chat_id, session)
            send_portfolio_menu(chat_id, "Social links berhasil diupdate.")
            return "ok"

        field = resolve_field_choice(cleaned, PORTFOLIO_SOCIAL_FIELDS)
        if not field:
            send_text(
                chat_id,
                format_field_menu(
                    "Pilihan field social links tidak dikenal.",
                    PORTFOLIO_SOCIAL_FIELDS,
                    "Ketik save untuk menyimpan perubahan social links.",
                ),
            )
            return "ok"
        session["selected_field"] = field
        session["step"] = "field_value"
        save_portfolio_session(chat_id, session)
        send_text(chat_id, prompt_social_field_value(field))
        return "ok"

    field = str(session.get("selected_field") or "")
    draft = dict(session.get("draft") or {})
    payload, field_error = update_social_field(draft, field, cleaned)
    if field_error or not payload:
        send_text(chat_id, field_error or "Nilai social links tidak valid.")
        return "ok"

    session["draft"] = payload
    session["step"] = "field_menu"
    session.pop("selected_field", None)
    save_portfolio_session(chat_id, session)
    send_text(
        chat_id,
        format_social_review(payload)
        + "\n\n"
        + format_field_menu(
            "Pilih field social links yang mau diubah:",
            PORTFOLIO_SOCIAL_FIELDS,
            "Ketik save untuk menyimpan perubahan social links.",
        ),
    )
    return "ok"


def handle_portfolio_session_input(chat_id: str, text: Optional[str]) -> str:
    session = get_portfolio_session(chat_id)
    if not session:
        return "ignored"

    user_text = (text or "").strip()
    lower = user_text.lower()

    if lower in {"", "help", "menu", "!porto"}:
        reset_portfolio_session(session)
        save_portfolio_session(chat_id, session)
        send_portfolio_menu(chat_id)
        return "ok"

    if lower in {"0", "exit", "keluar"}:
        clear_portfolio_session(chat_id)
        send_text(chat_id, "Sesi !porto ditutup.")
        return "ok"

    if lower in {"cancel", "batal"}:
        reset_portfolio_session(session)
        save_portfolio_session(chat_id, session)
        send_portfolio_menu(chat_id, "Flow !porto dibatalkan.")
        return "ok"

    if lower == "back":
        return handle_portfolio_back(chat_id, session)

    flow = str(session.get("flow") or "menu")

    if flow == "menu":
        return handle_portfolio_menu(chat_id, session, lower)
    if flow == "add_project":
        return handle_portfolio_add_project(chat_id, session, user_text)
    if flow == "edit_project":
        return handle_portfolio_edit_project(chat_id, session, user_text)
    if flow == "delete_project":
        return handle_portfolio_delete_project(chat_id, session, user_text)
    if flow == "edit_profile":
        return handle_portfolio_edit_profile(chat_id, session, user_text)
    if flow == "edit_social":
        return handle_portfolio_edit_social(chat_id, session, user_text)

    reset_portfolio_session(session)
    save_portfolio_session(chat_id, session)
    send_portfolio_menu(chat_id, "State !porto tidak dikenal. Kembali ke menu.")
    return "ok"


def handle_portfolio_command(chat_id: str, command: Optional[str], text: Optional[str]) -> str:
    if command == "porto":
        if get_post_draft(chat_id) or get_logbook_session(chat_id):
            send_text(chat_id, "Selesaikan sesi !post atau !logbook dulu sebelum membuka !porto.")
            return "ok"
        if not is_portfolio_chat_allowed(chat_id):
            logger.warning("Portfolio access denied for chat_id=%s", chat_id)
            send_text(chat_id, "Kamu tidak diizinkan memakai fitur portfolio ini.")
            return "ok"
        if not PORTFOLIO_API_BASE_URL or not PORTFOLIO_API_SECRET:
            send_text(chat_id, "Konfigurasi portfolio belum lengkap. Set PORTFOLIO_API_BASE_URL dan PORTFOLIO_API_SECRET.")
            return "error"
        session = {"status": "active", "flow": "menu", "step": "menu"}
        save_portfolio_session(chat_id, session)
        send_portfolio_menu(chat_id)
        return "ok"

    if not get_portfolio_session(chat_id):
        return "ignored"

    return handle_portfolio_session_input(chat_id, text)


def rate_limit_ok(chat_id: str) -> Tuple[bool, int]:
    now = time.time()
    last = rate_limit.get(chat_id)
    if last and (now - last) < RATE_LIMIT_SECONDS:
        remaining = int(RATE_LIMIT_SECONDS - (now - last))
        return False, max(1, remaining)
    rate_limit[chat_id] = now
    return True, 0


def sanitize_debug_error(error: Optional[str], limit: int = 300) -> str:
    if not error:
        return "Tidak ada detail error."
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
            "11) Mode logbook KP: !logbook",
            "12) Submit/cancel/update logbook: !ok / !cancel / !update",
            "13) Mode CRUD portfolio: !porto",
            "",
            "Catatan:",
            "- Bot ini dipakai lewat private chat Telegram",
            "- Data harga saham & IHSG via TradingView (tvDatafeed)",
            "- Berita dari Google News RSS",
            "- Output S/R berbasis pivot harian",
            "- AI chat umum via !ai, mentor backend via !explain, berita via !news",
            f"- LinkedIn post support caption + 1-{LINKEDIN_MAX_IMAGES} gambar",
            "- Logbook mode: isi materi manual, lalu konfirmasi submit ke MIS",
            "- Portfolio mode: edit data website lewat API portfolio",
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

    if command == "porto" or get_portfolio_session(chat_id):
        ok, remaining = rate_limit_ok(chat_id)
        if not ok:
            send_text(chat_id, f"Mohon tunggu {remaining} detik sebelum request lagi.")
            return "rate_limited"
        return handle_portfolio_command(chat_id, command, text)

    if is_logbook_command(command):
        ok, remaining = rate_limit_ok(chat_id)
        if not ok:
            send_text(chat_id, f"Mohon tunggu {remaining} detik sebelum request lagi.")
            return "rate_limited"
        return handle_logbook_command(chat_id, str(command))

    session = get_logbook_session(chat_id)
    if session:
        if session.get("status") == "awaiting_file":
            if media and handle_logbook_file_upload(chat_id, media):
                return "ok"
            if not media and not command:
                send_text(
                    chat_id,
                    "Kirim file PDF (laporan) atau foto JPG/JPEG (foto kegiatan), atau ketik !skip untuk selesai.",
                )
                return "logbook_file_waiting"
            if command and command != "logbook_skip":
                send_text(
                    chat_id,
                    "Kamu masih di mode unggah file logbook. Kirim file atau ketik !skip untuk selesai.",
                )
                return "logbook_file_waiting"
        elif command:
            send_text(
                chat_id,
                "Kamu masih di mode !logbook. Kirim teks kegiatan/materi, atau pakai !ok / !update / !cancel.",
            )
            return "logbook_mode_waiting"
        elif text and handle_logbook_mode_input(chat_id, text):
            return "ok"
        else:
            return "logbook_mode_waiting"

    if command in {"post", "postok", "cancelpost", "review"}:
        ok, remaining = rate_limit_ok(chat_id)
        if not ok:
            send_text(chat_id, f"Mohon tunggu {remaining} detik sebelum request lagi.")
            return "rate_limited"
        return handle_post_command(chat_id, str(command))

    if get_post_draft(chat_id) and command:
        send_text(
            chat_id,
            "Kamu masih di mode !post. Kirim caption/gambar, ketik !review untuk cek draft, !postok untuk publish, atau !cancelpost buat batal.",
        )
        return "post_mode_waiting"

    if handle_post_mode_input(chat_id, text, media):
        return "ok"

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

    if command == "explain":
        explain_text = re.sub(r"^!explain\s*", "", text, flags=re.IGNORECASE).strip()
        if not explain_text:
            send_text(chat_id, "Ketik: !explain <masalah backend yang mau dijelasin>")
            return "ok"
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
    if not os.getenv("LINKEDIN_ACCESS_TOKEN", "").strip():
        missing.append("LINKEDIN_ACCESS_TOKEN")
    if not os.getenv("LINKEDIN_AUTHOR_URN", "").strip():
        missing.append("LINKEDIN_AUTHOR_URN")
    if LOGBOOK_ENABLED:
        if not LOGBOOK_CAS_LOGIN_URL:
            missing.append("LOGBOOK_CAS_LOGIN_URL")
        if not LOGBOOK_FORM_URL:
            missing.append("LOGBOOK_FORM_URL")
        if not LOGBOOK_CAS_USERNAME:
            missing.append("LOGBOOK_CAS_USERNAME")
        if not LOGBOOK_CAS_PASSWORD:
            missing.append("LOGBOOK_CAS_PASSWORD")
    if PORTFOLIO_API_BASE_URL and not PORTFOLIO_API_SECRET:
        missing.append("PORTFOLIO_API_SECRET")

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
