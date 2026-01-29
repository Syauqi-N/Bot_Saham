import os
from typing import Any, Dict, List, Optional, Tuple

import requests

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "groq/compound-mini")
GROQ_API_URL = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")

_session = requests.Session()


def groq_chat(messages: List[Dict[str, str]], model: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY belum di-set. AI chat belum aktif."

    payload: Dict[str, Any] = {
        "model": model or GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 250,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        response = _session.post(GROQ_API_URL, json=payload, headers=headers, timeout=20)
        if response.status_code >= 400:
            return None, f"Groq error {response.status_code}: {response.text}"
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return content.strip(), None
    except Exception as exc:
        return None, f"Gagal memanggil Groq: {exc}"
