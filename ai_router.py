from pathlib import Path
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from backend_savior_client import backend_savior_chat
from groq_client import groq_chat

MAX_HISTORY = 30
_history: Dict[str, List[Dict[str, str]]] = {}
BACKEND_SAVIOR_MAX_HISTORY = 20
_backend_savior_history: Dict[str, List[Dict[str, str]]] = {}
BACKEND_SAVIOR_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

logger = logging.getLogger("bot_saham.ai_router")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


BACKEND_SAVIOR_FALLBACK_TO_GROQ = _env_bool("BACKEND_SAVIOR_FALLBACK_TO_GROQ", True)
BACKEND_SAVIOR_FALLBACK_MAX_TOKENS = max(100, _env_int("BACKEND_SAVIOR_FALLBACK_MAX_TOKENS", 450))


def _load_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = _load_system_prompt()
BACKEND_SAVIOR_SYSTEM_PROMPT = """
Kamu adalah Senior Backend Architect & Career Mentor untuk Syauqi.
Fokus bantu jelaskan logika backend dengan runtut, praktis, dan bisa langsung dipakai.

Aturan jawaban:
1. Breakdown Logic: jelaskan alur backend langkah per langkah.
2. Architecture: sarankan struktur folder/design pattern yang relevan.
3. Boilerplate: berikan contoh kode clean (Python atau Node.js) sesuai konteks user.
4. Tutup dengan dorongan positif singkat.
""".strip()


def _is_retryable_backend_savior_error(error: str) -> bool:
    lowered = error.lower()
    if "timeout" in lowered:
        return True
    if "connection" in lowered or "temporarily unavailable" in lowered:
        return True

    status_match = re.search(r"backend savior error (\d{3})", lowered)
    if not status_match:
        return False
    status_code = int(status_match.group(1))
    return status_code in BACKEND_SAVIOR_RETRYABLE_STATUS_CODES


def get_ai_reply(chat_id: str, user_text: str) -> Tuple[Optional[str], Optional[str]]:
    history = _history.get(chat_id, [])
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_text}
    ]

    reply, error = groq_chat(messages)
    if error:
        return None, error

    reply = " ".join(reply.splitlines()).strip()
    reply = re.sub(r"\s+", " ", reply)
    sentences = re.split(r"(?<=[.!?])\s+", reply)
    if len(sentences) > 4:
        reply = " ".join(sentences[:4]).strip()

    updated = (history + [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])[
        -MAX_HISTORY:
    ]
    _history[chat_id] = updated
    return reply, None


def summarize_news(query: Optional[str], articles: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not articles:
        return None, "Belum ada berita untuk diringkas."

    topic = query or "headline pasar hari ini"
    lines = []
    for i, article in enumerate(articles[:8], start=1):
        title = str(article.get("title", "")).strip()
        source = str(article.get("source", "")).strip() or "-"
        description = str(article.get("description", "")).strip() or "-"
        lines.append(f"{i}. {title} | Sumber: {source} | Detail: {description}")

    system_prompt = (
        "Kamu analis berita pasar Indonesia. "
        "Tulis ringkasan singkat dalam Bahasa Indonesia yang to the point."
    )
    user_prompt = "\n".join(
        [
            f"Topik: {topic}",
            "Ringkas berita berikut menjadi 3-5 bullet point.",
            "Aturan:",
            "- Fokus ke inti update, hindari clickbait.",
            "- Maksimal 1 kalimat per bullet.",
            "- Jika ada dampak ke saham/market, sebutkan singkat.",
            "- Jangan tambah fakta di luar input.",
            "",
            "Data berita:",
            *lines,
        ]
    )

    reply, error = groq_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    if error or not reply:
        return None, error or "Ringkasan AI gagal dibuat."

    reply = reply.strip()
    reply = re.sub(r"\n{3,}", "\n\n", reply)
    summary_lines = [line.rstrip() for line in reply.splitlines() if line.strip()]
    if len(summary_lines) > 7:
        reply = "\n".join(summary_lines[:7]).strip()
    return reply, None


def get_backend_savior_reply(chat_id: str, user_text: str) -> Tuple[Optional[str], Optional[str]]:
    history = _backend_savior_history.get(chat_id, [])
    messages = [{"role": "system", "content": BACKEND_SAVIOR_SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_text}
    ]

    reply, error = backend_savior_chat(messages)
    if error:
        can_fallback = (
            BACKEND_SAVIOR_FALLBACK_TO_GROQ
            and "BACKEND_SAVIOR_API_KEY" not in error
            and _is_retryable_backend_savior_error(error)
        )
        if not can_fallback:
            return None, error

        fallback_reply, fallback_error = groq_chat(
            messages,
            max_tokens=BACKEND_SAVIOR_FALLBACK_MAX_TOKENS,
        )
        if fallback_error or not fallback_reply:
            if fallback_error:
                logger.warning("Backend Savior failed and fallback Groq failed: %s | %s", error, fallback_error)
                return None, f"{error} | Fallback Groq gagal: {fallback_error}"
            return None, error

        logger.warning("Backend Savior failed, fallback to Groq used: %s", error)
        reply = fallback_reply

    reply = (reply or "").strip()
    if not reply:
        return None, "Respons Backend Savior kosong."

    updated = (
        history
        + [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
    )[-BACKEND_SAVIOR_MAX_HISTORY:]
    _backend_savior_history[chat_id] = updated
    return reply, None
