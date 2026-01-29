from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

from groq_client import groq_chat

MAX_HISTORY = 6
_history: Dict[str, List[Dict[str, str]]] = {}


def _load_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = _load_system_prompt()


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
