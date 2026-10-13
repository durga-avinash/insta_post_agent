from __future__ import annotations

import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .models import Story, TrendFeed, TrendSignal
from .normalize import normalize_timestamp, normalize_title
from .collector import feed_error_detail, make_session

LOGGER = logging.getLogger(__name__)
GOOGLE_TRENDS_NAMESPACE = "https://trends.google.com/trending/rss"
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "it", "of",
    "on", "or", "the", "to", "with", "after", "amid", "over", "under", "will", "has", "have",
    "new", "news", "live", "today", "latest", "update", "updates", "vs", "versus",
}


def load_trend_feeds(path: Path) -> list[TrendFeed]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    feeds = []
    for item in raw:
        geo = str(item["geo"]).strip().upper()
        region = str(item["region"]).strip().upper()
        name, url = str(item["name"]).strip(), str(item["url"]).strip()
        if not geo or region not in {"INDIA", "WORLD"} or not name or not url.startswith("https://"):
            raise ValueError(f"Invalid Google Trends feed configuration: {item!r}")
        feeds.append(TrendFeed(geo=geo, region=region, name=name, url=url))  # type: ignore[arg-type]
    return feeds


def parse_approximate_searches(value: str) -> int | None:
    """Return the lower-bound count from a Google Trends traffic bucket, not an exact count."""
    match = re.search(r"([\d,.]+)\s*([KMB])?\s*\+?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get((match.group(2) or "").upper(), 1)
    return int(amount * multiplier)


def _child_text(parent: ET.Element, local_name: str) -> str:
    for child in parent:
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return " ".join("".join(child.itertext()).split())
    return ""


def parse_trend_feed_content(
    feed: TrendFeed,
    content: bytes | str,
    *,
    retrieved_at: datetime | None = None,
    max_items: int = 30,
) -> list[TrendSignal]:
    """Parse the public Trending Now RSS export and retain Google-provided trend evidence."""
    root = ET.fromstring(content)
    channel = next((item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "channel"), root)
    signals: list[TrendSignal] = []
    for item in (node for node in channel.iter() if node.tag.rsplit("}", 1)[-1] == "item"):
        query = _child_text(item, "title")
        if not query:
            continue
        traffic = _child_text(item, "approx_traffic")
        news_items: list[dict[str, str]] = []
        for node in item:
            if node.tag.rsplit("}", 1)[-1] != "news_item":
                continue
            news_title = _child_text(node, "news_item_title")
            news_url = _child_text(node, "news_item_url")
            publisher = _child_text(node, "news_item_source")
            snippet = _child_text(node, "news_item_snippet")
            if news_title or news_url:
                news_items.append({"title": news_title, "url": news_url, "publisher": publisher, "snippet": snippet})
        started = normalize_timestamp(_child_text(item, "pubDate"))
        signals.append(
            TrendSignal(
                geo=feed.geo,
                region=feed.region,
                query=query,
                approx_traffic=traffic,
                approximate_searches=parse_approximate_searches(traffic),
                started_at=started,
                url=_child_text(item, "link"),
                news_items=news_items,
            )
        )
        if len(signals) >= max_items:
            break
    return signals


def collect_trend_signals(
    feeds: list[TrendFeed],
    *,
    session: requests.Session | None = None,
    min_delay_seconds: float = 0.25,
) -> tuple[list[TrendSignal], list[dict[str, str]]]:
    client = session or make_session()
    signals: list[TrendSignal] = []
    status: list[dict[str, str]] = []
    for index, feed in enumerate(feeds):
        if index and min_delay_seconds > 0:
            time.sleep(min_delay_seconds)
        try:
            response = client.get(feed.url, timeout=(8, 20))
            response.raise_for_status()
            parsed = parse_trend_feed_content(feed, response.content)
            signals.extend(parsed)
            status.append({"geo": feed.geo, "region": feed.region, "source_id": feed.name, "status": "OK", "items": str(len(parsed))})
        except (requests.RequestException, ET.ParseError, ValueError, UnicodeError) as exc:
            detail = feed_error_detail(exc)
            LOGGER.warning("Trend feed %s failed: %s", feed.geo, detail)
            status.append({"geo": feed.geo, "region": feed.region, "source_id": feed.name, "status": "FAILED", "items": "0", "error": detail})
    return signals, status


def fresh_trend_signals(signals: list[TrendSignal], *, now: datetime, max_age_hours: int = 24) -> tuple[list[TrendSignal], int]:
    clock = now.astimezone(timezone.utc)
    cutoff = clock - timedelta(hours=max_age_hours)
    fresh = [
        signal for signal in signals
        if signal.started_at is not None and cutoff <= signal.started_at.astimezone(timezone.utc) <= clock + timedelta(minutes=10)
    ]
    return fresh, len(signals) - len(fresh)


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[^\W_]{2,}", value.casefold(), flags=re.UNICODE)
        if token not in _STOP_WORDS
    }


def _match_strength(query: str, candidate: str) -> float:
    left, right = normalize_title(query), normalize_title(candidate)
    if not left or not right:
        # Retain support for non-Latin search terms using Unicode word tokens.
        left_tokens, right_tokens = _tokens(query), _tokens(candidate)
        if not left_tokens or not right_tokens:
            return 0.0
    else:
        left_tokens, right_tokens = _tokens(left), _tokens(right)
        if left in right or right in left:
            return 1.0
    overlap = left_tokens & right_tokens
    if not overlap:
        return 0.0
    if len(left_tokens) == 1:
        return 0.72 if overlap == left_tokens else 0.0
    recall = len(overlap) / len(left_tokens)
    precision = len(overlap) / max(1, len(right_tokens))
    sequence = SequenceMatcher(None, left, right).ratio()
    score = 0.78 * recall + 0.22 * precision
    if len(overlap) < 2 or recall < 0.5:
        return 0.0
    return min(0.99, max(score, sequence * 0.85))


def match_story_to_trends(story: Story, signals: list[TrendSignal], *, min_searches: int = 10_000) -> list[dict[str, object]]:
    """Match a story only to real, dated, volume-bearing search trends for the same region."""
    documents = [story.headline, *(article.title for article in story.articles)]
    matched: list[dict[str, object]] = []
    for signal in signals:
        if signal.region != story.region or signal.started_at is None:
            continue
        if signal.approximate_searches is None or signal.approximate_searches < min_searches:
            continue
        aliases = [signal.query, *(item.get("title", "") for item in signal.news_items)]
        best = (0.0, "", "")
        for alias in aliases:
            if not alias:
                continue
            for document in documents:
                strength = _match_strength(alias, document)
                if strength > best[0]:
                    best = (strength, alias, document)
        if best[0] < 0.55:
            continue
        matched.append(
            {
                "geo": signal.geo,
                "region": signal.region,
                "query": signal.query,
                "approx_traffic": signal.approx_traffic,
                "approximate_searches_lower_bound": signal.approximate_searches,
                "started_at": signal.started_at.isoformat(),
                "google_trends_url": signal.url,
                "matched_against": best[2],
                "matched_trend_text": best[1],
                "match_strength": round(best[0], 3),
                "related_news_items": signal.news_items,
            }
        )
    matched.sort(key=lambda item: (int(item["approximate_searches_lower_bound"]), float(item["match_strength"])), reverse=True)
    return matched


def apply_trend_evidence(story: Story, evidence: list[dict[str, object]]) -> None:
    story.trend_evidence = evidence
    if not evidence:
        story.trend_status = "UNCONFIRMED"
        return
    story.trend_status = "CONFIRMED_GOOGLE_TRENDS"
    max_searches = max(int(item["approximate_searches_lower_bound"]) for item in evidence)
    locations = {str(item["geo"]) for item in evidence}
    match_strength = max(float(item["match_strength"]) for item in evidence)
    search_component = round(min(70.0, 10.0 * math.log10(max_searches)), 2)
    location_component = float(min(20, max(0, len(locations) - 1) * 10))
    match_component = round(10.0 * match_strength, 2)
    story.score_components = {
        "google_trends_search_volume_lower_bound": float(max_searches),
        "google_trends_volume_rank_component": search_component,
        "trending_geographies": float(len(locations)),
        "cross_geography_component": location_component,
        "headline_trend_match_component": match_component,
    }
    story.trend_score = round(search_component + location_component + match_component, 2)

