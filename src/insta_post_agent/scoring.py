from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .models import Article
from .normalize import normalize_title, stable_id

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "it", "of", "on",
    "or", "says", "the", "to", "with", "after", "amid", "over", "under", "will", "has", "have", "new",
}


def deduplicate_articles(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    result = []
    for article in articles:
        key = article.canonical_url
        if key and key not in seen:
            seen.add(key)
            result.append(article)
    return result


def _tokens(title: str) -> set[str]:
    return {word for word in normalize_title(title).split() if word not in STOP_WORDS and len(word) > 1}


def titles_describe_same_event(left: str, right: str) -> bool:
    if normalize_title(left) == normalize_title(right):
        return True
    a, b = _tokens(left), _tokens(right)
    if min(len(a), len(b)) < 2:
        return False
    overlap = len(a & b) / len(a | b)
    containment = len(a & b) / min(len(a), len(b))
    sequence = SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()
    return overlap >= 0.50 or (containment >= 0.78 and sequence >= 0.55)


def cluster_articles(articles: list[Article]) -> list[list[Article]]:
    """Greedy title-based event groups; every grouping is retained in the manifest."""
    clusters: list[list[Article]] = []
    for article in deduplicate_articles(articles):
        best: list[Article] | None = None
        best_ratio = 0.0
        for cluster in clusters:
            same_region = any(item.region == article.region for item in cluster)
            if not same_region:
                continue
            for item in cluster:
                if item.region != article.region:
                    continue
                ratio = SequenceMatcher(None, normalize_title(item.title), normalize_title(article.title)).ratio()
                if titles_describe_same_event(item.title, article.title) and ratio >= best_ratio:
                    best, best_ratio = cluster, ratio
        if best is None:
            clusters.append([article])
        else:
            best.append(article)
    return clusters


def trend_score(articles: list[Article], now: datetime | None = None) -> tuple[float, dict[str, float]]:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    publishers = {article.source_domain for article in articles if article.source_domain}
    coverage = min(40.0, max(0, len(publishers) - 1) * 20.0)
    best_time = max((a.published_at for a in articles if a.published_at), default=None)
    if best_time is None:
        recency = 0.0
    else:
        age_hours = max(0.0, (clock - best_time).total_seconds() / 3600)
        recency = 35.0 * math.exp(-age_hours / 30.0)
    text = " ".join(f"{a.title} {a.description}" for a in articles).lower()
    significance = 8.0
    if any(word in text for word in ("government", "court", "central bank", "election", "national")):
        significance += 8.0
    if any(word in text for word in ("climate", "health", "economy", "security", "education", "research")):
        significance += 5.0
    if any(word in text for word in ("breaking", "major", "historic", "urgent")):
        significance += 4.0
    significance = min(25.0, significance)
    components = {"recency": round(recency, 2), "independent_coverage_proxy": coverage, "public_significance_proxy": significance, "search_interest": 0.0}
    return round(sum(components.values()), 2), components


def cluster_key(articles: list[Article]) -> str:
    preferred = min((normalize_title(a.title) for a in articles), default="unknown event")
    region = min((a.region for a in articles), default="WORLD")
    return stable_id(region, preferred)
