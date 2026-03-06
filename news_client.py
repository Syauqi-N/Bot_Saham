"""
News client for fetching latest headlines from multiple RSS sources.
"""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urlencode
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger("news_client")

GOOGLE_NEWS_RSS = "https://news.google.com/rss"
WIB = timezone(timedelta(hours=7))


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_csv(name: str, default_csv: str) -> List[str]:
    raw = os.getenv(name, default_csv)
    return [item.strip() for item in raw.split(",") if item.strip()]


HTTP_TIMEOUT = max(3, _env_int("NEWS_HTTP_TIMEOUT", 8))
NEWS_RELAX_DAYS = max(2, _env_int("NEWS_RELAX_DAYS", 7))
NEWS_PER_SOURCE_MULTIPLIER = max(2, _env_int("NEWS_PER_SOURCE_MULTIPLIER", 3))
NEWS_SITE_SOURCES = _env_csv(
    "NEWS_SITE_SOURCES",
    "cnbcindonesia.com,kontan.co.id,bisnis.com,idxchannel.com",
)
NEWS_DIRECT_FEEDS = _env_csv(
    "NEWS_DIRECT_FEEDS",
    "https://www.antaranews.com/rss/ekonomi.xml",
)

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


def _clean_text(value: Optional[str]) -> str:
    text = unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _local_name(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _normalize_title(value: str) -> str:
    return re.sub(r"\W+", " ", value.lower()).strip()


def _query_tokens(query: Optional[str]) -> List[str]:
    if not query:
        return []
    return [token for token in re.split(r"[^a-z0-9]+", query.lower()) if len(token) >= 2]


def _query_score(text: str, tokens: Sequence[str]) -> int:
    lowered = text.lower()
    return sum(1 for token in tokens if token in lowered)


def _is_stock_related(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    has_stock_keyword = any(keyword in text for keyword in STOCK_KEYWORDS)
    has_sports_keyword = any(keyword in text for keyword in SPORTS_KEYWORDS)
    if has_sports_keyword and not has_stock_keyword:
        return False
    return has_stock_keyword


def _parse_pubdate(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    parsed: Optional[datetime] = None
    try:
        parsed = parsedate_to_datetime(value)
    except Exception:
        parsed = None

    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            parsed = None

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_pubdate(value: Optional[str]) -> Optional[str]:
    parsed = _parse_pubdate(value)
    if parsed is None:
        return value
    return parsed.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB")


def _build_google_query(
    query: Optional[str],
    *,
    days: Optional[int],
    include_stock_context: bool,
    site: Optional[str] = None,
) -> str:
    stock_context = '(saham OR emiten OR idx OR bei OR ihsg OR lq45 OR "pasar modal" OR "harga saham" OR tbk)'
    sports_exclude = '-bola -"sepak bola" -fc -liga -"super league" -pertandingan -"prediksi skor" -"live streaming" -vs'

    parts: List[str] = []
    if query and query.strip():
        parts.append(f"({query.strip()})")
    if site:
        parts.append(f"site:{site.strip()}")
    if include_stock_context:
        parts.append(stock_context)
    if days is not None and days > 0:
        parts.append(f"when:{days}d")
    parts.append(sports_exclude)
    return " ".join(parts).strip()


def _build_google_url(query: str) -> str:
    params = {"hl": "id", "gl": "ID", "ceid": "ID:id", "q": query}
    return f"{GOOGLE_NEWS_RSS}/search?{urlencode(params)}"


def _build_source_plan(query: Optional[str], relaxed: bool) -> List[Tuple[str, str]]:
    days = None if relaxed else 1
    include_stock_context = not relaxed

    plans: List[Tuple[str, str]] = []
    google_query = _build_google_query(
        query,
        days=days,
        include_stock_context=include_stock_context,
    )
    if google_query:
        plans.append(("google", _build_google_url(google_query)))

    for site in NEWS_SITE_SOURCES:
        site_query = _build_google_query(
            query,
            days=days,
            include_stock_context=include_stock_context,
            site=site,
        )
        if site_query:
            plans.append((f"google:{site}", _build_google_url(site_query)))

    for index, raw_feed in enumerate(NEWS_DIRECT_FEEDS, start=1):
        feed_url = raw_feed
        if "{query}" in feed_url:
            feed_url = feed_url.replace("{query}", quote_plus(query or ""))
        plans.append((f"feed:{index}", feed_url))

    return plans


def _extract_child_text(item: ET.Element, names: Sequence[str]) -> str:
    target_names = {name.lower() for name in names}
    for child in list(item):
        if _local_name(child.tag) in target_names:
            text = _clean_text(" ".join(child.itertext()))
            if text:
                return text
    return ""


def _extract_link(item: ET.Element) -> str:
    link_text = _extract_child_text(item, ("link",))
    if link_text:
        return link_text

    first_href = ""
    for child in list(item):
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if not href:
            continue
        rel = str(child.attrib.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href
        if not first_href:
            first_href = href
    return first_href


def _extract_items(root: ET.Element) -> List[ET.Element]:
    rss_items = root.findall("./channel/item")
    if rss_items:
        return rss_items

    # Atom fallback.
    atom_entries = root.findall(".//{*}entry")
    if atom_entries:
        return atom_entries

    generic: List[ET.Element] = []
    for node in root.findall(".//*"):
        if _local_name(node.tag) in {"item", "entry"}:
            generic.append(node)
    return generic


def _source_label_from_name(source_name: str) -> str:
    if source_name == "google":
        return "Google News"
    if source_name.startswith("google:"):
        return source_name.split(":", 1)[1]
    return source_name


def _parse_source_articles(source_name: str, xml_bytes: bytes, per_source_limit: int) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items = _extract_items(root)
    source_label = _source_label_from_name(source_name)

    results: List[Dict[str, Any]] = []
    for item in items[:per_source_limit]:
        title = _extract_child_text(item, ("title",))
        link = _extract_link(item)
        description = _extract_child_text(item, ("description", "summary", "content"))
        published_raw = _extract_child_text(item, ("pubDate", "published", "updated", "date"))
        source = _extract_child_text(item, ("source", "author", "creator"))

        # Google News RSS sering menaruh source di akhir title.
        if not source and " - " in title:
            head, tail = title.rsplit(" - ", 1)
            if head and tail:
                title = head.strip()
                source = tail.strip()

        if not source:
            source = source_label

        sort_dt = _parse_pubdate(published_raw)
        sort_ts = sort_dt.timestamp() if sort_dt else 0.0
        results.append(
            {
                "title": title or "(Tanpa judul)",
                "source": source,
                "link": link,
                "description": description,
                "published": _format_pubdate(published_raw),
                "_sort_ts": sort_ts,
            }
        )

    return results


def _fetch_source_articles(source_name: str, url: str, per_source_limit: int) -> List[Dict[str, Any]]:
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
            logger.warning(
                "News source failed (%s): HTTP %s",
                source_name,
                response.status_code,
            )
            return []
        return _parse_source_articles(source_name, response.content, per_source_limit)
    except (requests.exceptions.RequestException, ET.ParseError) as exc:
        logger.warning("News source parse/request error (%s): %s", source_name, exc)
        return []
    except Exception as exc:
        logger.warning("News source unexpected error (%s): %s", source_name, exc)
        return []


def _dedupe_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[str, Dict[str, Any]] = {}
    for article in articles:
        key = (article.get("link") or "").strip()
        if not key:
            key = _normalize_title(str(article.get("title", "")))
        if not key:
            continue

        existing = deduped.get(key)
        if not existing:
            deduped[key] = article
            continue

        if article.get("_sort_ts", 0.0) > existing.get("_sort_ts", 0.0):
            deduped[key] = article

    return list(deduped.values())


def _finalize_articles(
    articles: List[Dict[str, Any]],
    query: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    stock_filtered = [
        article
        for article in articles
        if _is_stock_related(str(article.get("title", "")), str(article.get("description", "")))
    ]
    if not stock_filtered:
        return []

    tokens = _query_tokens(query)
    for article in stock_filtered:
        text = f"{article.get('title', '')} {article.get('description', '')}"
        article["_query_score"] = _query_score(text, tokens) if tokens else 0

    relevant = [article for article in stock_filtered if article.get("_query_score", 0) > 0]
    pool = relevant if relevant else stock_filtered
    pool.sort(key=lambda item: (item.get("_query_score", 0), item.get("_sort_ts", 0.0)), reverse=True)

    final: List[Dict[str, Any]] = []
    for article in pool[:limit]:
        clean_article = dict(article)
        clean_article.pop("_query_score", None)
        clean_article.pop("_sort_ts", None)
        final.append(clean_article)
    return final


def _collect_plan_articles(query: Optional[str], limit: int, relaxed: bool) -> List[Dict[str, Any]]:
    source_plan = _build_source_plan(query, relaxed=relaxed)
    per_source_limit = max(limit * NEWS_PER_SOURCE_MULTIPLIER, 8)
    target_collected = max(limit * 2, limit * NEWS_PER_SOURCE_MULTIPLIER * 2)

    collected: List[Dict[str, Any]] = []
    for source_name, source_url in source_plan:
        collected.extend(_fetch_source_articles(source_name, source_url, per_source_limit))
        if len(collected) >= target_collected:
            break

    if not collected:
        return []

    deduped = _dedupe_articles(collected)
    return _finalize_articles(deduped, query, limit)


def fetch_news(query: Optional[str], limit: int = 5) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if limit <= 0:
        return None, "Limit berita harus lebih dari 0."

    normalized_query = (query or "").strip() or None

    strict_results = _collect_plan_articles(normalized_query, limit, relaxed=False)
    if strict_results:
        return strict_results, None

    relaxed_results = _collect_plan_articles(normalized_query, limit, relaxed=True)
    if relaxed_results:
        return relaxed_results, None

    # Final fallback: broad market news when specific query is too niche.
    if normalized_query:
        broad_results = _collect_plan_articles(None, limit, relaxed=True)
        if broad_results:
            return broad_results, None

    return None, "Belum ada berita saham yang cocok untuk topik ini."
