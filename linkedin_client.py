import base64
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


LINKEDIN_ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
LINKEDIN_AUTHOR_URN = os.getenv("LINKEDIN_AUTHOR_URN", "")
LINKEDIN_API_BASE_URL = os.getenv("LINKEDIN_API_BASE_URL", "https://api.linkedin.com").rstrip("/")
LINKEDIN_TIMEOUT_CONNECT = _env_int("LINKEDIN_TIMEOUT_CONNECT", 10)
LINKEDIN_TIMEOUT_READ = _env_int("LINKEDIN_TIMEOUT_READ", 45)

_session = requests.Session()


def _json_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _register_upload() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    url = f"{LINKEDIN_API_BASE_URL}/v2/assets?action=registerUpload"
    payload: Dict[str, Any] = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "owner": LINKEDIN_AUTHOR_URN,
            "serviceRelationships": [
                {
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent",
                }
            ],
        }
    }
    try:
        response = _session.post(
            url,
            json=payload,
            headers=_json_headers(),
            timeout=(LINKEDIN_TIMEOUT_CONNECT, LINKEDIN_TIMEOUT_READ),
        )
        if response.status_code >= 400:
            return None, None, f"LinkedIn register upload error {response.status_code}: {response.text[:400]}"
        data = response.json()
        value = data.get("value") or {}
        upload_mechanism = value.get("uploadMechanism") or {}
        upload_http = upload_mechanism.get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest") or {}
        upload_url = upload_http.get("uploadUrl")
        asset = value.get("asset")
        if not upload_url or not asset:
            return None, None, "LinkedIn register upload response tidak punya uploadUrl/asset."
        return str(upload_url), str(asset), None
    except requests.exceptions.RequestException as exc:
        return None, None, f"Gagal register upload LinkedIn: {exc}"
    except (TypeError, ValueError, KeyError) as exc:
        return None, None, f"Format register upload LinkedIn tidak valid: {exc}"


def _upload_binary(upload_url: str, media_bytes: bytes, mimetype: str) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": mimetype or "application/octet-stream",
    }
    try:
        response = _session.put(
            upload_url,
            data=media_bytes,
            headers=headers,
            timeout=(LINKEDIN_TIMEOUT_CONNECT, LINKEDIN_TIMEOUT_READ),
        )
        if response.status_code in {405, 501}:
            response = _session.post(
                upload_url,
                data=media_bytes,
                headers=headers,
                timeout=(LINKEDIN_TIMEOUT_CONNECT, LINKEDIN_TIMEOUT_READ),
            )
        if response.status_code >= 400:
            return f"LinkedIn upload error {response.status_code}: {response.text[:300]}"
        return None
    except requests.exceptions.RequestException as exc:
        return f"Gagal upload media ke LinkedIn: {exc}"


def _candidate_media_urls(media_url: str, waha_base_url: Optional[str]) -> Tuple[str, ...]:
    raw = (media_url or "").strip()
    if not raw:
        return ()

    candidates = []
    parsed = urlparse(raw)

    if not parsed.scheme:
        base = (waha_base_url or "").rstrip("/") + "/"
        if base:
            candidates.append(urljoin(base, raw.lstrip("/")))
        else:
            candidates.append(raw)
    else:
        candidates.append(raw)

    if not waha_base_url:
        return tuple(dict.fromkeys(candidates))

    base_parsed = urlparse(waha_base_url)
    if not base_parsed.scheme or not base_parsed.netloc:
        return tuple(dict.fromkeys(candidates))

    parsed_first = urlparse(candidates[0])
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0"}
    if parsed_first.hostname in local_hosts:
        replaced = urlunparse(
            (
                base_parsed.scheme,
                base_parsed.netloc,
                parsed_first.path,
                parsed_first.params,
                parsed_first.query,
                parsed_first.fragment,
            )
        )
        candidates.append(replaced)

    # WAHA versions/configs may expose files as either:
    # - /api/files/<session>/<filename>
    # - /api/files/<filename>
    expanded = list(candidates)
    for item in list(candidates):
        parsed_item = urlparse(item)
        path = parsed_item.path or ""
        parts = path.split("/")
        # ['', 'api', 'files', '<session>', '<filename...>']
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "files" and parts[3]:
            short_path = "/api/files/" + "/".join(parts[4:])
            expanded.append(
                urlunparse(
                    (
                        parsed_item.scheme,
                        parsed_item.netloc,
                        short_path,
                        parsed_item.params,
                        parsed_item.query,
                        parsed_item.fragment,
                    )
                )
            )

    return tuple(dict.fromkeys(expanded))


def _download_media_from_url(
    media_url: str,
    waha_api_key: Optional[str],
    waha_base_url: Optional[str],
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    headers: Dict[str, str] = {}
    if waha_api_key:
        headers["X-API-Key"] = waha_api_key
        headers["Authorization"] = f"Bearer {waha_api_key}"
    attempts = _candidate_media_urls(media_url, waha_base_url)
    if not attempts:
        return None, None, "URL media WAHA kosong."

    errors = []
    for candidate in attempts:
        try:
            response = _session.get(
                candidate,
                headers=headers,
                timeout=(LINKEDIN_TIMEOUT_CONNECT, LINKEDIN_TIMEOUT_READ),
            )
            if response.status_code >= 400:
                errors.append(f"{candidate} -> HTTP {response.status_code}")
                continue
            content_type = str(response.headers.get("Content-Type", "application/octet-stream")).split(";")[0]
            return response.content, content_type, None
        except requests.exceptions.RequestException as exc:
            errors.append(f"{candidate} -> {exc}")

    return None, None, "Gagal download media WAHA: " + " | ".join(errors[:2])


def _decode_media_base64(media_data: str) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        return base64.b64decode(media_data, validate=True), None
    except Exception as exc:
        return None, f"Gagal decode media base64: {exc}"


def _create_ugc_image_post(caption: str, asset: str) -> Tuple[Optional[str], Optional[str]]:
    payload: Dict[str, Any] = {
        "author": LINKEDIN_AUTHOR_URN,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": caption},
                "shareMediaCategory": "IMAGE",
                "media": [
                    {
                        "status": "READY",
                        "media": asset,
                        "title": {"text": "Auto post"},
                    }
                ],
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    url = f"{LINKEDIN_API_BASE_URL}/v2/ugcPosts"
    try:
        response = _session.post(
            url,
            json=payload,
            headers=_json_headers(),
            timeout=(LINKEDIN_TIMEOUT_CONNECT, LINKEDIN_TIMEOUT_READ),
        )
        if response.status_code >= 400:
            return None, f"LinkedIn create post error {response.status_code}: {response.text[:400]}"
        restli_id = response.headers.get("X-RestLi-Id")
        if restli_id:
            return str(restli_id), None
        try:
            body = response.json()
        except ValueError:
            body = {}
        post_id = body.get("id")
        return (str(post_id), None) if post_id else ("", None)
    except requests.exceptions.RequestException as exc:
        return None, f"Gagal membuat post LinkedIn: {exc}"


def create_linkedin_image_post(
    caption: str,
    media_url: Optional[str] = None,
    media_data_base64: Optional[str] = None,
    media_mimetype: Optional[str] = None,
    waha_api_key: Optional[str] = None,
    waha_base_url: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if not LINKEDIN_ACCESS_TOKEN:
        return None, "LINKEDIN_ACCESS_TOKEN belum di-set."
    if not LINKEDIN_AUTHOR_URN:
        return None, "LINKEDIN_AUTHOR_URN belum di-set."
    if not caption.strip():
        return None, "Caption kosong."
    if not media_url and not media_data_base64:
        return None, "Media image belum tersedia."

    media_bytes: Optional[bytes] = None
    resolved_mimetype = (media_mimetype or "").strip() or "image/jpeg"

    if media_data_base64:
        media_bytes, decode_error = _decode_media_base64(media_data_base64)
        if decode_error:
            return None, decode_error
    elif media_url:
        media_bytes, detected_mime, download_error = _download_media_from_url(
            media_url,
            waha_api_key,
            waha_base_url,
        )
        if download_error:
            return None, download_error
        if detected_mime:
            resolved_mimetype = detected_mime

    if not media_bytes:
        return None, "Konten media kosong."

    upload_url, asset, register_error = _register_upload()
    if register_error or not upload_url or not asset:
        return None, register_error or "Gagal register upload ke LinkedIn."

    upload_error = _upload_binary(upload_url, media_bytes, resolved_mimetype)
    if upload_error:
        return None, upload_error

    return _create_ugc_image_post(caption.strip(), asset)
