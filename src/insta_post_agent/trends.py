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

from .models import Article, FeedSource, Story, TrendFeed, TrendSignal
from .normalize import canonicalize_url, normalize_domain, normalize_timestamp, normalize_title, stable_id
from .collector import feed_error_detail, make_session, parse_feed_content
from .scoring import titles_describe_same_event
from .verification import is_trusted_source_domain

LOGGER = logging.getLogger(__name__)
GOOGLE_TRENDS_NAMESPACE = "https://trends.google.com/trending/rss"
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "it", "of",
    "on", "or", "the", "to", "with", "after", "amid", "over", "under", "will", "has", "have",
    "new", "news", "live", "today", "latest", "update", "updates", "vs", "versus", "fixture", "only",
}


def trend_news_source_id(signal: TrendSignal) -> str:
    """Stable feed identity for one region/query response from Google News RSS."""
    return f"google-news-trend-{signal.geo.lower()}-{stable_id(normalize_title(signal.query))}"


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


def collect_trend_news_articles(
    signals: list[TrendSignal],
    *,
    min_searches: int = 10_000,
    max_queries_per_geo: int = 10,
    max_items_per_query: int = 20,
    session: requests.Session | None = None,
    retrieved_at: datetime | None = None,
    min_delay_seconds: float = 0.25,
) -> tuple[list[Article], list[dict[str, str]]]:
    """Collect dated publisher RSS coverage for current, high-volume trend topics."""
    if max_queries_per_geo < 1 or max_items_per_query < 1:
        raise ValueError("Trend news query and item limits must be positive")
    client = session or make_session()
    clock = retrieved_at or datetime.now(timezone.utc)
    locale = {
        "IN": ("INDIA", "en-IN", "IN", "IN:en"),
        "US": ("WORLD", "en-US", "US", "US:en"),
        "GB": ("WORLD", "en-GB", "GB", "GB:en"),
    }
    selected: list[TrendSignal] = []
    by_geo: dict[str, list[TrendSignal]] = {}
    for signal in signals:
        # Avoid spending requests on low-volume topics which cannot qualify for a
        # slide under the same threshold used by the editorial trend gate.
        if signal.approximate_searches is None or signal.approximate_searches < min_searches or signal.started_at is None:
            continue
        if signal.geo not in locale:
            continue
        by_geo.setdefault(signal.geo, []).append(signal)
    for geo, group in by_geo.items():
        group.sort(key=lambda signal: (signal.approximate_searches or 0, signal.started_at or clock), reverse=True)
        seen: set[str] = set()
        for signal in group:
            key = normalize_title(signal.query)
            if not key or key in seen:
                continue
            seen.add(key)
            selected.append(signal)
            if len(seen) >= max_queries_per_geo:
                break

    articles: list[Article] = []
    status: list[dict[str, str]] = []
    for index, signal in enumerate(selected):
        if index and min_delay_seconds > 0:
            time.sleep(min_delay_seconds)
        region, language, country, edition = locale[signal.geo]
        source = FeedSource(
            id=trend_news_source_id(signal),
            name=f"Google News RSS ({signal.geo}: {signal.query})",
            url="https://news.google.com/rss/search",
            region=region,  # type: ignore[arg-type]
            section="Trending news search",
            source_type="google_news_search",
        )
        try:
            response = client.get(
                source.url,
                params={"q": signal.query, "hl": language, "gl": country, "ceid": edition},
                timeout=(8, 20),
            )
            response.raise_for_status()
            found = parse_feed_content(source, response.content, retrieved_at=clock, max_items=max_items_per_query)
            articles.extend(found)
            status.append({"geo": signal.geo, "query": signal.query, "status": "OK", "items": str(len(found))})
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            detail = feed_error_detail(exc)
            LOGGER.warning("Trend news query %s (%s) failed: %s", signal.geo, signal.query, detail)
            status.append({"geo": signal.geo, "query": signal.query, "status": "FAILED", "items": "0", "error": detail})
    return articles, status


def collect_candidate_coverage(
    stories: list[Story],
    *,
    now: datetime,
    max_queries_per_region: int = 6,
    max_items_per_query: int = 15,
    session: requests.Session | None = None,
    min_delay_seconds: float = 0.25,
) -> tuple[dict[str, list[Article]], list[dict[str, str]]]:
    """Check targeted Google News RSS pickup for leading current stories.

    This is a bounded, free RSS search fallback for days when Google Trends is
    dominated by unrelated topics. It measures publisher coverage, not search
    volume, public sentiment, or social-media activity.
    """
    if max_queries_per_region < 1 or max_items_per_query < 1:
        raise ValueError("Coverage query and item limits must be positive")
    client = session or make_session()
    clock = now.astimezone(timezone.utc)
    cutoff = clock - timedelta(hours=24)
    locale = {
        "INDIA": ("en-IN", "IN", "IN:en"),
        "WORLD": ("en-US", "US", "US:en"),
    }
    selected: list[Story] = []
    for region in ("INDIA", "WORLD"):
        eligible = [
            story for story in stories
            if story.region == region
            and story.headline.strip()
            and any(
                is_trusted_source_domain(article.source_domain)
                and article.description.strip()
                and article.published_at is not None
                and cutoff <= article.published_at.astimezone(timezone.utc) <= clock + timedelta(minutes=10)
                for article in story.articles
            )
        ]
        eligible.sort(
            key=lambda story: (
                story.trend_score,
                max((article.published_at for article in story.articles if article.published_at), default=datetime.min.replace(tzinfo=timezone.utc)),
            ),
            reverse=True,
        )
        selected.extend(eligible[:max_queries_per_region])

    coverage_by_story: dict[str, list[Article]] = {}
    status: list[dict[str, str]] = []
    for index, story in enumerate(selected):
        if index and min_delay_seconds > 0:
            time.sleep(min_delay_seconds)
        language, country, edition = locale[story.region]
        query = " ".join(story.headline.split())[:180].rsplit(" ", 1)[0]
        source = FeedSource(
            id=f"google-news-coverage-{stable_id(story.news_id, normalize_title(query))}",
            name="Google News RSS (targeted publisher coverage)",
            url="https://news.google.com/rss/search",
            region=story.region,
            section="Targeted coverage check",
            source_type="google_news_search",
        )
        try:
            response = client.get(
                source.url,
                params={
                    "q": f'{query} when:2d',
                    "hl": language,
                    "gl": country,
                    "ceid": edition,
                },
                timeout=(8, 20),
            )
            response.raise_for_status()
            parsed = parse_feed_content(source, response.content, retrieved_at=clock, max_items=max_items_per_query)
            original_urls = {
                canonicalize_url(article.canonical_url or article.url)
                for article in story.articles
                if article.url
            }
            original_domains = {
                normalize_domain(article.source_domain) for article in story.articles if article.source_domain
            }
            seen_urls = set(original_urls)
            matched: list[Article] = []
            for article in parsed:
                url_key = canonicalize_url(article.canonical_url or article.url)
                if (
                    not is_trusted_source_domain(article.source_domain)
                    or not article.description.strip()
                    or article.published_at is None
                    or not cutoff <= article.published_at.astimezone(timezone.utc) <= clock + timedelta(minutes=10)
                    or normalize_domain(article.source_domain) in original_domains
                    or url_key in seen_urls
                    or not titles_describe_same_event(story.headline, article.title)
                ):
                    continue
                seen_urls.add(url_key)
                matched.append(article)
            if matched:
                coverage_by_story[story.news_id] = matched
            status.append({
                "news_id": story.news_id,
                "region": story.region,
                "headline": story.headline,
                "status": "OK",
                "items": str(len(parsed)),
                "trusted_matching_publishers": str(len({article.source_domain for article in matched})),
            })
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            detail = feed_error_detail(exc)
            LOGGER.warning("Google News coverage query failed for %s: %s", story.news_id, detail)
            status.append({
                "news_id": story.news_id,
                "region": story.region,
                "headline": story.headline,
                "status": "FAILED",
                "items": "0",
                "trusted_matching_publishers": "0",
                "error": detail,
            })
    return coverage_by_story, status


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
        # A broad one-word trend such as "girls" or "championship" is not enough
        # to establish that an article is about the searched event. Exact related-
        # news URL evidence is handled separately below.
        if len(left_tokens) < 2:
            return 0.0
        if left in right:
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
    if len(overlap) < 2 or recall < 0.67:
        return 0.0
    return min(0.99, max(score, sequence * 0.85))


def merge_specific_trend_coverage(
    clusters: list[list[Article]],
    signals: list[TrendSignal],
) -> list[list[Article]]:
    """Group distinct publisher reports returned for one specific current trend query.

    Google News RSS gives every result in a query response the same feed ID. The
    query itself can therefore link two differently worded reports about one
    sufficiently specific event. Generic queries are deliberately excluded;
    verification still checks each original publisher URL independently.
    """
    result = [list(cluster) for cluster in clusters]
    for signal in signals:
        if len(_tokens(signal.query)) < 3:
            continue
        source_id = trend_news_source_id(signal)
        linked_urls = {
            canonicalize_url(item.get("url", ""))
            for item in signal.news_items
            if item.get("url")
        }
        related = [
            article
            for cluster in result
            for article in cluster
            if article.source_id == source_id
            and article.source_type == "google_news_search"
            and article.region == signal.region
            and (
                _match_strength(signal.query, article.title) >= 0.55
                or canonicalize_url(article.canonical_url or article.url) in linked_urls
            )
        ]
        related_by_url = {
            canonicalize_url(article.canonical_url or article.url): article
            for article in related
        }
        if len(related_by_url) < 2:
            continue
        related_keys = set(related_by_url)
        touched_indexes = [
            index for index, cluster in enumerate(result)
            if any(canonicalize_url(article.canonical_url or article.url) in related_keys for article in cluster)
        ]
        merged: list[Article] = []
        seen_urls: set[str] = set()
        for index in touched_indexes:
            for article in result[index]:
                key = canonicalize_url(article.canonical_url or article.url)
                if key and key not in seen_urls:
                    seen_urls.add(key)
                    merged.append(article)
        for article in related:
            key = canonicalize_url(article.canonical_url or article.url)
            if key and key not in seen_urls:
                seen_urls.add(key)
                merged.append(article)
        if touched_indexes:
            for index in reversed(touched_indexes):
                result.pop(index)
        result.append(merged)
    return result


def match_story_to_trends(story: Story, signals: list[TrendSignal], *, min_searches: int = 10_000) -> list[dict[str, object]]:
    """Match a story only to real, dated, volume-bearing search trends for the same region."""
    documents = [story.headline, *(article.title for article in story.articles)]
    documents.extend(article.description for article in story.articles if article.description.strip())
    matched: list[dict[str, object]] = []
    for signal in signals:
        if signal.region != story.region or signal.started_at is None:
            continue
        if signal.approximate_searches is None or signal.approximate_searches < min_searches:
            continue
        # Text matching uses the Trends query itself. Related publisher headlines
        # are useful for exact URL corroboration, but using them as loose aliases
        # can make unrelated stories look trending just because they share a word.
        aliases = [signal.query]
        best = (0.0, "", "", "headline_text_similarity", "")
        for alias in aliases:
            if not alias:
                continue
            for document in documents:
                strength = _match_strength(alias, document)
                if strength > best[0]:
                    best = (strength, alias, document, "headline_text_similarity", "")
        # Google Trends provides related publisher links. An exact canonical URL
        # match is stronger than title similarity and survives headline rewrites.
        article_urls = {
            canonicalize_url(article.canonical_url or article.url): article.title
            for article in story.articles
            if canonicalize_url(article.canonical_url or article.url)
        }
        for item in signal.news_items:
            linked_url = canonicalize_url(item.get("url", ""))
            if linked_url and linked_url in article_urls:
                linked_title = item.get("title", "") or signal.query
                best = (1.0, linked_title, article_urls[linked_url], "google_trends_related_news_url", linked_url)
                break
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
                "match_method": best[3],
                "matched_news_item_url": best[4],
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

