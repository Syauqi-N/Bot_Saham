"""
News client for fetching latest headlines from Google News RSS.
"""

from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger("news_client")

GOOGLE_NEWS_RSS = "https://news.google.com/rss"
HTTP_TIMEOUT = 20
WIB = timezone(timedelta(hours=7))

STOCK_KEYWORDS = (
    "saham",
    "emiten",
    "idx",
    "bei",
    "ihsg",
    "lq45",
    "pasar modal",
    "bursa",
    "tbk",
    "ipo",
    "dividen",
    "buyback",
    "right issue",
    "harga saham",
    "stock",
)

SPORTS_KEYWORDS = (
    "sepak bola",
    "super league",
    "premier league",
    "liga 1",
    "liga ",
    "pertandingan",
    "kick-off",
    "live streaming",
    "prediksi skor",
    "gol",
    "timnas",
    "fc",
    "vs ",
    " detiksport",
)

_session = requests.Session()


def _build_news_url(query: Optional[str]) -> str:
    params = {"hl": "id", "gl": "ID", "ceid": "ID:id"}
    stock_context = "(saham OR emiten OR idx OR bei OR ihsg OR lq45 OR \"pasar modal\" OR \"harga saham\" OR tbk)"
    sports_exclude = "-bola -\"sepak bola\" -fc -liga -\"super league\" -pertandingan -\"prediksi skor\" -\"live streaming\" -vs"
    if query and query.strip():
        params["q"] = f"({query.strip()}) {stock_context} when:1d {sports_exclude}"
    else:
        params["q"] = f"{stock_context} when:1d {sports_exclude}"
    return f"{GOOGLE_NEWS_RSS}/search?{urlencode(params)}"


def _clean_text(value: Optional[str]) -> str:
    text = unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _format_pubdate(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB")
    except Exception:
        return value


def _is_stock_related(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    has_stock_keyword = any(keyword in text for keyword in STOCK_KEYWORDS)
    has_sports_keyword = any(keyword in text for keyword in SPORTS_KEYWORDS)
    if has_sports_keyword and not has_stock_keyword:
        return False
    return has_stock_keyword


def fetch_news(query: Optional[str], limit: int = 5) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if limit <= 0:
        return None, "Limit berita harus lebih dari 0."

    url = _build_news_url(query)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        response = _session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        if response.status_code >= 400:
            logger.error("Google News RSS error: %s %s", response.status_code, response.text)
            return None, f"Gagal mengambil berita (HTTP {response.status_code})."

        root = ET.fromstring(response.content)
        items = root.findall("./channel/item") or root.findall(".//item")
        if not items:
            return None, "Belum ada berita yang cocok."

        results: List[Dict[str, Any]] = []
        for item in items[:limit]:
            title = _clean_text(item.findtext("title"))
            link = _clean_text(item.findtext("link"))
            description = _clean_text(item.findtext("description"))
            published = _format_pubdate(item.findtext("pubDate"))

            source = ""
            source_node = item.find("source")
            if source_node is not None and source_node.text:
                source = _clean_text(source_node.text)

            # Google News RSS sering menaruh source di akhir title.
            if not source and " - " in title:
                head, tail = title.rsplit(" - ", 1)
                if head and tail:
                    title = head.strip()
                    source = tail.strip()

            results.append(
                {
                    "title": title or "(Tanpa judul)",
                    "source": source,
                    "link": link,
                    "description": description,
                    "published": published,
                }
            )

        if not results:
            return None, "Belum ada berita yang bisa ditampilkan."

        # Safety net: keep only stock-related articles.
        filtered = [article for article in results if _is_stock_related(article["title"], article.get("description", ""))]
        if not filtered:
            return None, "Belum ada berita saham yang cocok untuk topik ini."

        return filtered[:limit], None

    except requests.exceptions.Timeout:
        return None, "Request berita timeout. Coba lagi sebentar."
    except requests.exceptions.RequestException as exc:
        logger.exception("News request error: %s", exc)
        return None, "Gagal mengambil berita. Coba lagi nanti."
    except ET.ParseError as exc:
        logger.exception("RSS parse error: %s", exc)
        return None, "Format data berita tidak valid."
    except Exception as exc:
        logger.exception("Unexpected news error: %s", exc)
        return None, "Error tidak terduga saat ambil berita."
