from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}
TRACKING_PREFIXES = ("utm_",)


def normalize_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(candidate)
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(html.unescape(url.strip()))
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if (parts.scheme.lower(), parts.port) in {("http", 80), ("https", 443)}:
        host = host.split(":", 1)[0]
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower in TRACKING_KEYS or any(lower.startswith(p) for p in TRACKING_PREFIXES):
            continue
        query_items.append((key, value))
    query_items.sort()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(query_items), ""))


def normalize_domain(value: str) -> str:
    """Normalize publisher hosts so www and bare-domain copies are one outlet."""
    raw = value.strip().lower().rstrip(".")
    host = urlsplit(raw).hostname if "://" in raw else raw.split(":", 1)[0]
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def normalize_title(title: str) -> str:
    text = html.unescape(title).lower()
    text = re.sub(r"\b(live updates?|live blog|latest updates?)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def stable_id(*parts: str, length: int = 16) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def clean_html_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())
