import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


BACKEND_SAVIOR_API_KEY = os.getenv("BACKEND_SAVIOR_API_KEY", "")
BACKEND_SAVIOR_MODEL = os.getenv("BACKEND_SAVIOR_MODEL", "z-ai/glm5")
BACKEND_SAVIOR_BASE_URL = os.getenv("BACKEND_SAVIOR_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
BACKEND_SAVIOR_MAX_TOKENS = _env_int("BACKEND_SAVIOR_MAX_TOKENS", 700)
BACKEND_SAVIOR_TIMEOUT_CONNECT = _env_int("BACKEND_SAVIOR_TIMEOUT_CONNECT", 10)
BACKEND_SAVIOR_TIMEOUT_READ = _env_int("BACKEND_SAVIOR_TIMEOUT_READ", 45)
BACKEND_SAVIOR_RETRIES = max(0, _env_int("BACKEND_SAVIOR_RETRIES", 2))
BACKEND_SAVIOR_RETRY_BACKOFF_SECONDS = max(0.0, _env_float("BACKEND_SAVIOR_RETRY_BACKOFF_SECONDS", 1.2))

_session = requests.Session()
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def backend_savior_chat(messages: List[Dict[str, str]], model: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    if not BACKEND_SAVIOR_API_KEY:
        return None, "BACKEND_SAVIOR_API_KEY belum di-set. Fitur !explain belum aktif."

    payload: Dict[str, Any] = {
        "model": model or BACKEND_SAVIOR_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": BACKEND_SAVIOR_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {BACKEND_SAVIOR_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{BACKEND_SAVIOR_BASE_URL}/chat/completions"

    last_error: Optional[str] = None
    for attempt in range(BACKEND_SAVIOR_RETRIES + 1):
        try:
            response = _session.post(
                url,
                json=payload,
                headers=headers,
                timeout=(BACKEND_SAVIOR_TIMEOUT_CONNECT, BACKEND_SAVIOR_TIMEOUT_READ),
            )
            if response.status_code >= 400:
                body = response.text[:500]
                last_error = f"Backend Savior error {response.status_code}: {body}"
                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < BACKEND_SAVIOR_RETRIES:
                    delay = BACKEND_SAVIOR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                    if delay > 0:
                        time.sleep(delay)
                    continue
                return None, last_error

            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return str(content).strip(), None

        except requests.exceptions.Timeout as exc:
            last_error = (
                "Gagal memanggil Backend Savior: timeout "
                f"(connect={BACKEND_SAVIOR_TIMEOUT_CONNECT}s, read={BACKEND_SAVIOR_TIMEOUT_READ}s): {exc}"
            )
            if attempt < BACKEND_SAVIOR_RETRIES:
                delay = BACKEND_SAVIOR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                if delay > 0:
                    time.sleep(delay)
                continue
            return None, last_error
        except requests.exceptions.RequestException as exc:
            last_error = f"Gagal memanggil Backend Savior: {exc}"
            if attempt < BACKEND_SAVIOR_RETRIES:
                delay = BACKEND_SAVIOR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                if delay > 0:
                    time.sleep(delay)
                continue
            return None, last_error
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return None, f"Format respons Backend Savior tidak valid: {exc}"
        except Exception as exc:
            return None, f"Gagal memanggil Backend Savior: {exc}"

    return None, last_error or "Gagal memanggil Backend Savior: unknown error"
