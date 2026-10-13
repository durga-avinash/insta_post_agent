from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .classify import classify_category, classify_region
from .models import Article, FeedSource
from .normalize import canonicalize_url, clean_html_text, stable_id

LOGGER = logging.getLogger(__name__)


def feed_error_detail(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return f"HTTP {status}" if status is not None else exc.__class__.__name__


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "InstaPostAgent/0.1 (RSS reader; contact: repository owner)"})
    return session


def _entry_timestamp(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except (OverflowError, TypeError, ValueError):
                return None
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            except ValueError:
                try:
                    value = parsedate_to_datetime(str(raw))
                    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    return None
    return None


def parse_feed_content(
    source: FeedSource,
    content: bytes | str,
    *,
    retrieved_at: datetime | None = None,
    max_items: int = 40,
) -> list[Article]:
    now = retrieved_at or datetime.now(timezone.utc)
    parsed = feedparser.parse(content)
    articles: list[Article] = []
    for entry in parsed.entries[:max_items]:
        title = clean_html_text(str(entry.get("title", "")))
        link = str(entry.get("link", "")).strip()
        canonical = canonicalize_url(link)
        if not title or not canonical:
            continue
        summary_raw = str(entry.get("summary", ""))
        if not summary_raw and entry.get("content"):
            summary_raw = str(entry.content[0].get("value", ""))
        description = clean_html_text(summary_raw)
        domain = urlsplit(canonical).hostname or ""
        article_id = stable_id(source.id, str(entry.get("id", "")) or canonical)
        region = classify_region(source.region, title, description)
        articles.append(
            Article(
                id=article_id,
                title=title,
                publisher=source.name,
                url=link,
                canonical_url=canonical,
                published_at=_entry_timestamp(entry),
                retrieved_at=now.astimezone(timezone.utc),
                region=region,  # type: ignore[arg-type]
                category=classify_category(title, description),
                description=description,
                source_id=source.id,
                source_type=source.source_type,
                source_domain=domain,
            )
        )
    return articles


def collect_sources(
    sources: list[FeedSource],
    *,
    max_items: int = 40,
    min_delay_seconds: float = 0.25,
    session: requests.Session | None = None,
) -> tuple[list[Article], list[dict[str, str]]]:
    client = session or make_session()
    articles: list[Article] = []
    feed_status: list[dict[str, str]] = []
    for index, source in enumerate(sources):
        if index and min_delay_seconds > 0:
            time.sleep(min_delay_seconds)
        try:
            response = client.get(source.url, timeout=(8, 20))
            response.raise_for_status()
            entries = parse_feed_content(source, response.content, max_items=max_items)
            articles.extend(entries)
            feed_status.append({"source_id": source.id, "status": "OK", "items": str(len(entries))})
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            detail = feed_error_detail(exc)
            LOGGER.warning("Feed %s failed: %s", source.id, detail)
            feed_status.append({"source_id": source.id, "status": "FAILED", "items": "0", "error": detail})
    return articles, feed_status
