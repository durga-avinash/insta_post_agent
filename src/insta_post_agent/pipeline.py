from __future__ import annotations

import json
import logging
import os
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from .caption import make_caption
from .collector import collect_sources
from .classify import is_mba_relevant, mba_relevance_score
from .history import NewsHistory
from .models import Article, Story, TrendSignal
from .normalize import normalize_domain, normalize_title
from .render import render_slide
from .scoring import cluster_articles, cluster_key, deduplicate_articles
from .sources import load_sources
from .verification import build_story, is_trusted_source_domain, source_independence_groups
from .workbook import create_workbook, validate_workbook
from .trends import (
    apply_trend_evidence,
    collect_candidate_coverage,
    collect_trend_news_articles,
    collect_trend_signals,
    fresh_trend_signals,
    load_trend_feeds,
    merge_specific_trend_coverage,
    match_story_to_trends,
)

LOGGER = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
MAX_STORIES_PER_REGION = 3
MAX_SLIDES = 6
DEFAULT_MIN_TREND_SEARCHES = 10_000
MIN_INDEPENDENT_PUBLISHERS_FOR_COVERAGE_SIGNAL = 3
CONFIRMED_TREND_STATUSES = frozenset({"CONFIRMED_GOOGLE_TRENDS", "CONFIRMED_CROSS_PUBLISHER_COVERAGE"})


def _caption_section(story: Story) -> str:
    links = list(dict.fromkeys(article.url for article in story.articles if article.url))
    if story.original_url and story.original_url not in links:
        links.insert(0, story.original_url)
    return f"{story.headline}\n{story.summary}\nSources: {story.publisher}\n" + "\n".join(links)


def _fresh_articles(articles: list[Article], now: datetime, max_age_hours: int) -> tuple[list[Article], int, int]:
    cutoff = now - timedelta(hours=max_age_hours)
    fresh, stale, future = [], 0, 0
    for article in articles:
        if article.published_at is None:
            fresh.append(article)
        elif article.published_at < cutoff:
            stale += 1
        elif article.published_at > now + timedelta(minutes=10):
            future += 1
        else:
            fresh.append(article)
    return fresh, stale, future


def _eligible_review_draft(story: Story) -> bool:
    """Allow evidence-backed current items into the editor queue, never as approved posts.

    Verification flags belong in the package and slide badge. Requiring a clean automated
    verification here caused the empty-carousel failure: items needing human review were
    discarded before an editor could inspect them. Unknown-only or evidence-free items
    still cannot enter the draft queue.
    """
    if story.verification_status != "NEEDS_REVIEW":
        return False
    if not story.original_url.startswith("https://") or not story.publisher.strip() or not story.summary.strip():
        return False
    if not story.articles or not story.evidence:
        return False
    trusted_reports = [
        article for article in story.articles
        if is_trusted_source_domain(article.source_domain)
        and article.description.strip()
        and article.published_at is not None
    ]
    if not trusted_reports:
        return False
    reject_terms = ("sponsored", "advertisement", "quiz", "horoscope", "newsletter", "podcast")
    return not any(term in story.headline.casefold() for term in reject_terms)


def _eligible_editorial_backup(story: Story, now: datetime) -> bool:
    """Allow a clearly labeled source-backed draft when trends are off-topic.

    This keeps a useful review package available without calling an article
    trending when the public trend feed has no matching MBA-relevant topic.
    """
    if story.verification_status == "REJECTED":
        return False
    if not story.original_url.startswith("https://") or not story.headline.strip():
        return False
    if not story.summary.strip() or story.summary.startswith("Source descriptions were unavailable"):
        return False
    reject_terms = ("sponsored", "advertisement", "quiz", "horoscope", "newsletter", "podcast")
    if any(term in story.headline.casefold() for term in reject_terms):
        return False
    cutoff = now - timedelta(hours=24)
    return any(
        is_trusted_source_domain(article.source_domain)
        and article.url.startswith("https://")
        and article.description.strip()
        and article.published_at is not None
        and cutoff <= article.published_at.astimezone(timezone.utc) <= now + timedelta(minutes=10)
        for article in story.articles
    )


def _coverage_profile(story: Story) -> tuple[int, int]:
    """Count trusted publisher domains and independent groups for ranking."""
    trusted_urls = {
        article.url for article in story.articles
        if is_trusted_source_domain(article.source_domain)
    }
    domains = {
        normalize_domain(article.source_domain) for article in story.articles
        if article.url in trusted_urls
    }
    independent_groups = {
        evidence.independence for evidence in story.evidence
        if evidence.url in trusted_urls and evidence.independence
    }
    return len(domains), len(independent_groups)


def _cross_publisher_coverage_signal(story: Story, now: datetime) -> dict[str, object] | None:
    """Return a cited coverage signal from recent curated reports, never a search count.

    Three distinct trusted outlets in the latest 24 hours can substitute for an
    unavailable/off-topic Google Trends match. Known wire copies and identical
    descriptions collapse into one source group, so syndication cannot inflate it.
    """
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=24)
    reports = [
        article for article in story.articles
        if article.published_at is not None
        and cutoff <= article.published_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc) + timedelta(minutes=10)
        and article.description.strip()
        and article.url.startswith("https://")
        and is_trusted_source_domain(article.source_domain)
    ]
    groups = source_independence_groups(reports)
    group_to_articles: dict[str, list[Article]] = {}
    for article in reports:
        group = groups.get(article.canonical_url or article.id, normalize_domain(article.source_domain))
        group_to_articles.setdefault(group, []).append(article)
    independent_groups = sorted(group_to_articles)
    publishers = sorted({normalize_domain(article.source_domain) for article in reports if article.source_domain})
    if len(publishers) < MIN_INDEPENDENT_PUBLISHERS_FOR_COVERAGE_SIGNAL or len(independent_groups) < MIN_INDEPENDENT_PUBLISHERS_FOR_COVERAGE_SIGNAL:
        return None
    evidence_urls = list(dict.fromkeys(
        article.url for group in independent_groups for article in group_to_articles[group]
    ))
    return {
        "signal_type": "independent_publisher_coverage",
        "window_hours": 24,
        "independent_publisher_count": len(independent_groups),
        "publisher_domains": publishers,
        "evidence_urls": evidence_urls,
        "interpretation": "High cross-publisher coverage proxy; not a search-volume or social-media measurement",
    }


def _has_recent_report(story: Story, now: datetime, max_age_hours: int = 24) -> bool:
    cutoff = now - timedelta(hours=max_age_hours)
    return any(
        article.published_at is not None
        and is_trusted_source_domain(article.source_domain)
        and cutoff <= article.published_at.astimezone(timezone.utc) <= now + timedelta(minutes=10)
        for article in story.articles
    )


def _is_mba_story(story: Story) -> bool:
    return any(is_mba_relevant(article.title, article.description) for article in story.articles)


def _mba_score(story: Story) -> float:
    return max((mba_relevance_score(article.title, article.description) for article in story.articles), default=0.0)


def _has_confirmed_trend_signal(story: Story) -> bool:
    return story.trend_status in CONFIRMED_TREND_STATUSES


def _best_unused_trend_evidence(story: Story, used_topics: set[str]) -> dict[str, object] | None:
    ranked = sorted(
        story.trend_evidence,
        key=lambda item: (
            int(item.get("approximate_searches_lower_bound", 0)),
            float(item.get("match_strength", 0.0)),
        ),
        reverse=True,
    )
    return next(
        (item for item in ranked if normalize_title(str(item.get("query", ""))) not in used_topics),
        None,
    )


def _make_summary(
    *,
    date_text: str,
    candidates: list[Story],
    selected: list[Story],
    slide_results: list[dict[str, object]],
    workbook_ok: bool,
    zip_ok: bool,
    warnings: list[str],
    history_persistence: str,
    text_layout_valid: bool,
    min_trend_searches: int,
    matched_by_region: dict[str, int],
    coverage_by_region: dict[str, int],
    fallback_by_region: dict[str, int],
    region_diagnostics: dict[str, dict[str, int]],
    trends_collected: int,
    fresh_trends: int,
    trend_news_articles_collected: int,
    trend_news_feed_status: list[dict[str, str]],
    candidate_coverage_feed_status: list[dict[str, str]],
    candidate_coverage_articles_collected: int,
) -> dict[str, object]:
    slide_count = len(slide_results)
    selected_by_region = {
        region: sum(story.region == region for story in selected)
        for region in ("INDIA", "WORLD")
    }
    slide_limit_respected = (
        slide_count <= MAX_SLIDES
        and all(count <= MAX_STORIES_PER_REGION for count in selected_by_region.values())
    )
    publication_ready = bool(selected) and all(
        story.verification_status == "APPROVED" and _has_confirmed_trend_signal(story)
        for story in selected
    )
    trend_matched_review_needed = any(
        story.verification_status == "NEEDS_REVIEW" and _has_confirmed_trend_signal(story)
        for story in candidates
    )
    return {
        "batch_date": date_text,
        "candidates_collected": sum(len(s.articles) for s in candidates),
        "distinct_story_clusters": len(candidates),
        "mba_relevant_clusters": sum(_is_mba_story(story) for story in candidates),
        "stories_approved": sum(s.verification_status == "APPROVED" for s in candidates),
        "stories_needs_review": sum(s.verification_status == "NEEDS_REVIEW" for s in candidates),
        "stories_rejected": sum(s.verification_status == "REJECTED" for s in candidates),
        "stories_selected": len(selected),
        "stories_selected_approved": sum(s.verification_status == "APPROVED" for s in selected),
        "stories_selected_needs_review": sum(s.verification_status == "NEEDS_REVIEW" for s in selected),
        "images_generated": slide_count,
        "max_slides": MAX_SLIDES,
        "max_stories_per_region": MAX_STORIES_PER_REGION,
        "minimum_google_trends_searches": min_trend_searches,
        "google_trend_signals_collected": trends_collected,
        "google_trend_signals_fresh": fresh_trends,
        "trend_news_articles_collected": trend_news_articles_collected,
        "google_trend_news_feed_status": trend_news_feed_status,
        "targeted_coverage_articles_collected": candidate_coverage_articles_collected,
        "targeted_coverage_queries": candidate_coverage_feed_status,
        "matched_trends_by_region": matched_by_region,
        "cross_publisher_coverage_signals_by_region": coverage_by_region,
        "unconfirmed_fallbacks_by_region": fallback_by_region,
        "region_selection_diagnostics": region_diagnostics,
        "selected_story_status": [
            {
                "slide_region": story.region,
                "headline": story.headline,
                "verification_status": story.verification_status,
                "trend_status": story.trend_status,
                "selection_basis": story.selection_basis,
                "trend_evidence": [
                    {
                        "geo": item.get("geo"),
                        "query": item.get("query"),
                        "approx_traffic": item.get("approx_traffic"),
                        "match_method": item.get("match_method"),
                        "signal_type": item.get("signal_type"),
                        "independent_publisher_count": item.get("independent_publisher_count"),
                    }
                    for item in story.trend_evidence
                ],
                "human_review_required": story.verification_status == "NEEDS_REVIEW" or not _has_confirmed_trend_signal(story),
            }
            for story in selected
        ],
        "selected_by_region": selected_by_region,
        "six_slide_maximum_respected": slide_limit_respected,
        "all_selected_slides_generated": slide_count == len(selected),
        "slide_limit_respected": slide_limit_respected,
        "excel_validated": workbook_ok,
        "images_layout_valid": text_layout_valid,
        "zip_created": zip_ok,
        "publication_ready": publication_ready,
        "human_review_required": trend_matched_review_needed or any(
            s.verification_status != "APPROVED" or not _has_confirmed_trend_signal(s) for s in selected
        ),
        "trend_selection_mode": (
            "MIXED_TREND_AND_EDITORIAL_BACKUP" if any(story.selection_basis == "EDITORIAL_BACKUP" for story in selected) and any(_has_confirmed_trend_signal(story) for story in selected)
            else "EDITORIAL_BACKUP_NO_TREND" if any(story.selection_basis == "EDITORIAL_BACKUP" for story in selected)
            else "MULTI_SIGNAL_TREND" if any(story.trend_status == "CONFIRMED_CROSS_PUBLISHER_COVERAGE" for story in selected) and any(story.trend_status == "CONFIRMED_GOOGLE_TRENDS" for story in selected)
            else "CROSS_PUBLISHER_COVERAGE" if any(story.trend_status == "CONFIRMED_CROSS_PUBLISHER_COVERAGE" for story in selected)
            else "TRENDS_MATCHED_REVIEW_DRAFT" if any(story.verification_status == "NEEDS_REVIEW" for story in selected)
            else "GOOGLE_TRENDS_MATCHED" if selected
            else "TRENDS_NEED_SOURCE_REVIEW" if trend_matched_review_needed
            else "NO_QUALIFYING_TREND_STORIES"
        ),
        "content_available": bool(selected),
        "batch_status": (
            "READY_FOR_REVIEW" if publication_ready
            else "REVIEW_DRAFT_AVAILABLE" if selected
            else "NO_QUALIFYING_TREND_STORIES"
        ),
        "workflow_complete": slide_count == len(selected) and slide_limit_respected and workbook_ok and zip_ok and text_layout_valid,
        "history_persistence": history_persistence,
        "warnings": warnings,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def run_daily(
    *,
    root: Path,
    output_root: Path,
    history_path: Path,
    now: datetime | None = None,
    sources_path: Path | None = None,
    sources: list | None = None,
    feed_client=None,
    max_age_hours: int | None = None,
    history_days: int | None = None,
    max_feed_items: int | None = None,
    trend_signals: list[TrendSignal] | None = None,
    min_trend_searches: int | None = None,
    allow_repeat_batch: bool | None = None,
) -> dict[str, object]:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_date = clock.astimezone(IST).date().isoformat()
    batch_id = local_date.replace("-", "")
    max_age = max_age_hours if max_age_hours is not None else int(os.getenv("NEWS_MAX_AGE_HOURS", "36"))
    repeat_days = history_days if history_days is not None else int(os.getenv("NEWS_HISTORY_DAYS", "2"))
    per_feed = max_feed_items if max_feed_items is not None else int(os.getenv("NEWS_MAX_FEED_ITEMS", "40"))
    threshold_value = min_trend_searches if min_trend_searches is not None else os.getenv("NEWS_MIN_TREND_SEARCHES", str(DEFAULT_MIN_TREND_SEARCHES))
    try:
        min_searches = int(threshold_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("NEWS_MIN_TREND_SEARCHES must be a positive whole number") from exc
    if min_searches < 1:
        raise ValueError("NEWS_MIN_TREND_SEARCHES must be a positive whole number")
    allow_repeat = allow_repeat_batch if allow_repeat_batch is not None else os.getenv("NEWS_ALLOW_REPEAT_BATCH", "false").lower() in {"1", "true", "yes"}
    state = NewsHistory(history_path)
    prior_batch = state.completed_batch(batch_id)
    if prior_batch is not None and not allow_repeat:
        state.close()
        return {
            **prior_batch,
            "workflow_complete": True,
            "duplicate_batch": True,
            "new_artifacts_created": False,
            "batch_status": "ALREADY_COMPLETED",
            "warnings": [f"Batch {batch_id} already completed; this redundant run was safely skipped. Download the artifact from the earlier run."],
        }
    sources = sources if sources is not None else load_sources(sources_path or root / "config" / "sources.json")
    raw_articles, feed_status = collect_sources(sources, max_items=per_feed, session=feed_client)
    warnings: list[str] = []
    if trend_signals is None:
        trend_feeds = load_trend_feeds(root / "config" / "trend_sources.json")
        raw_trends, trend_feed_status = collect_trend_signals(trend_feeds, session=feed_client)
    else:
        raw_trends = trend_signals
        trend_feed_status = [{"source_id": "injected", "status": "OK", "items": str(len(raw_trends))}]
    current_trends, stale_trend_count = fresh_trend_signals(raw_trends, now=clock, max_age_hours=24)
    if trend_signals is None:
        try:
            max_trend_queries = int(os.getenv("NEWS_TREND_QUERIES_PER_GEO", "20"))
        except ValueError as exc:
            raise ValueError("NEWS_TREND_QUERIES_PER_GEO must be a positive whole number") from exc
        if max_trend_queries < 1:
            raise ValueError("NEWS_TREND_QUERIES_PER_GEO must be a positive whole number")
        try:
            max_trend_articles = int(os.getenv("NEWS_TREND_ARTICLES_PER_QUERY", "20"))
        except ValueError as exc:
            raise ValueError("NEWS_TREND_ARTICLES_PER_QUERY must be a positive whole number") from exc
        if max_trend_articles < 1:
            raise ValueError("NEWS_TREND_ARTICLES_PER_QUERY must be a positive whole number")
        trend_news_articles, trend_news_feed_status = collect_trend_news_articles(
            current_trends,
            min_searches=min_searches,
            max_queries_per_geo=max_trend_queries,
            max_items_per_query=max_trend_articles,
            session=feed_client,
            retrieved_at=clock,
        )
    else:
        # Injected trend fixtures keep unit tests deterministic and network-free.
        trend_news_articles, trend_news_feed_status = [], []
    candidate_coverage_feed_status: list[dict[str, str]] = []
    candidate_coverage_articles_collected = 0
    raw_articles.extend(trend_news_articles)
    articles = deduplicate_articles(raw_articles)
    fresh_articles, stale_count, future_count = _fresh_articles(articles, clock, max_age)
    if stale_count:
        warnings.append(f"Ignored {stale_count} candidate articles older than the {max_age}-hour freshness window")
    if future_count:
        warnings.append(f"Ignored {future_count} articles with publication timestamps more than 10 minutes in the future")
    failed_feeds = [entry["source_id"] for entry in feed_status if entry["status"] != "OK"]
    if failed_feeds:
        warnings.append("Feeds unavailable: " + ", ".join(failed_feeds))
    failed_trend_searches = [
        entry.get("geo", "unknown") + ": " + entry.get("query", "")
        for entry in trend_news_feed_status
        if entry.get("status") != "OK"
    ]
    if failed_trend_searches:
        warnings.append("Google News trend searches unavailable: " + "; ".join(failed_trend_searches[:8]))
    if not fresh_articles:
        warnings.append("No recent source items were available; no story content was invented")
    if trend_signals is None and not trend_news_articles and current_trends:
        warnings.append("Google Trends was available, but its current topics yielded no usable dated Google News RSS coverage")
    if stale_trend_count:
        warnings.append(f"Ignored {stale_trend_count} undated, stale, or future Google Trends signal(s)")
    failed_trend_feeds = [entry.get("geo", entry.get("source_id", "unknown")) for entry in trend_feed_status if entry.get("status") != "OK"]
    if failed_trend_feeds:
        warnings.append("Google Trends feeds unavailable: " + ", ".join(failed_trend_feeds))
    if not current_trends:
        warnings.append("No fresh Google Trends evidence was obtained; no story will be labeled trending or promoted to a slide")

    clusters = merge_specific_trend_coverage(cluster_articles(fresh_articles), current_trends)
    candidates = [build_story(group, batch_id, now=clock) for group in clusters if group]
    for story in candidates:
        apply_trend_evidence(story, match_story_to_trends(story, current_trends, min_searches=min_searches))
    candidates.sort(key=lambda story: story.trend_score, reverse=True)

    # Google Trends can be irrelevant to the MBA audience on a given day. Search
    # the leading current business/policy stories individually through Google's
    # public News RSS endpoint and use the publisher pickup only if the result is
    # a real, recent, trusted, event-matched report. The search count is bounded
    # and never represented as a popularity or social-media measurement.
    if trend_signals is None:
        try:
            coverage_queries = int(os.getenv("NEWS_COVERAGE_QUERIES_PER_REGION", "6"))
        except ValueError as exc:
            raise ValueError("NEWS_COVERAGE_QUERIES_PER_REGION must be a positive whole number") from exc
        if coverage_queries < 1:
            raise ValueError("NEWS_COVERAGE_QUERIES_PER_REGION must be a positive whole number")
        coverage_search_pool = [
            story for story in candidates
            if _is_mba_story(story)
            and _has_recent_report(story, clock)
            and _eligible_editorial_backup(story, clock)
        ]
        coverage_by_story, candidate_coverage_feed_status = collect_candidate_coverage(
            coverage_search_pool,
            now=clock,
            max_queries_per_region=coverage_queries,
            session=feed_client,
        )
        candidate_coverage_articles_collected = sum(
            len(articles_found) for articles_found in coverage_by_story.values()
        )
        if coverage_by_story:
            augmented: list[Story] = []
            for story in candidates:
                additions = coverage_by_story.get(story.news_id, [])
                if not additions:
                    augmented.append(story)
                    continue
                enriched = build_story([*story.articles, *additions], batch_id, now=clock)
                # Keep the stable event ID already used for trend matching and
                # history; a newly discovered alternate headline must not change it.
                enriched.news_id = story.news_id
                apply_trend_evidence(
                    enriched,
                    match_story_to_trends(enriched, current_trends, min_searches=min_searches),
                )
                augmented.append(enriched)
            candidates = augmented
            candidates.sort(key=lambda story: story.trend_score, reverse=True)
        failed_coverage_searches = [
            f"{entry.get('region', 'unknown')}: {entry.get('headline', '')}"
            for entry in candidate_coverage_feed_status
            if entry.get("status") != "OK"
        ]
        if failed_coverage_searches:
            warnings.append("Targeted publisher coverage searches unavailable: " + "; ".join(failed_coverage_searches[:6]))
        if not candidate_coverage_feed_status:
            warnings.append("No targeted publisher coverage searches ran; no cross-publisher search fallback was available")

    history_persistence = "local SQLite file; GitHub Actions cache is best-effort and can expire"
    state.prune(now=clock)
    selected: list[Story] = []
    selected_event_keys: list[tuple[str, str]] = []
    matched_by_region: dict[str, int] = {}
    coverage_by_region: dict[str, int] = {}
    fallback_by_region: dict[str, int] = {}
    region_diagnostics: dict[str, dict[str, int]] = {}
    for region in ("INDIA", "WORLD"):
        regional_clusters = [story for story in candidates if story.region == region]
        recent_stories = [
            story for story in regional_clusters
            if _has_recent_report(story, clock) and _is_mba_story(story)
        ]
        confirmed = [story for story in recent_stories if story.trend_status == "CONFIRMED_GOOGLE_TRENDS"]
        if not allow_repeat:
            confirmed = [
                story for story in confirmed
                if not state.seen_recently(
                    cluster_key(story.articles),
                    signature="\n".join(article.title for article in story.articles),
                    days=repeat_days,
                    now=clock,
                )
            ]
        matched_by_region[region] = len(confirmed)
        fallback_by_region[region] = 0
        coverage_candidates: list[tuple[Story, dict[str, object]]] = []
        for story in recent_stories:
            if story.trend_status == "CONFIRMED_GOOGLE_TRENDS" or not _eligible_editorial_backup(story, clock):
                continue
            signal = _cross_publisher_coverage_signal(story, clock)
            if signal is not None:
                coverage_candidates.append((story, signal))
        coverage_by_region[region] = len(coverage_candidates)
        region_diagnostics[region] = {
            "fresh_story_clusters": len(regional_clusters),
            "recent_trusted_mba_story_clusters": len(recent_stories),
            "google_trends_matched_candidates": len(confirmed),
            "independent_coverage_candidates": len(coverage_candidates),
            "selected_for_carousel": 0,
        }
        approved_pool = [story for story in confirmed if story.verification_status == "APPROVED"]
        review_pool = [story for story in confirmed if _eligible_review_draft(story)]
        # Trending, fully corroborated stories rank first. Any current trend-matched
        # story with trusted source evidence can fill review slots, even when a date,
        # conflict, or sensitivity flag prevents automated approval. It remains a draft.
        approved_pool.sort(
            key=lambda story: (
                max(int(item["approximate_searches_lower_bound"]) for item in story.trend_evidence),
                len({str(item["geo"]) for item in story.trend_evidence}),
                _mba_score(story),
                story.trend_score,
            ),
            reverse=True,
        )
        review_pool.sort(
            key=lambda story: (
                max(int(item["approximate_searches_lower_bound"]) for item in story.trend_evidence),
                len({str(item["geo"]) for item in story.trend_evidence}),
                _mba_score(story),
                story.trend_score,
            ),
            reverse=True,
        )
        pool = approved_pool + review_pool
        kept = 0
        used_trend_topics: set[str] = set()
        for story in pool:
            chosen_trend = _best_unused_trend_evidence(story, used_trend_topics)
            if chosen_trend is None:
                continue
            key = cluster_key(story.articles)
            signature = "\n".join(article.title for article in story.articles)
            if not allow_repeat and state.seen_recently(key, signature=signature, days=repeat_days, now=clock):
                story.freshness_status = "RECENTLY_SELECTED"
                continue
            chosen_topic = normalize_title(str(chosen_trend.get("query", "")))
            used_trend_topics.add(chosen_topic)
            story.trend_evidence = [chosen_trend] + [item for item in story.trend_evidence if item is not chosen_trend]
            story.selection_basis = "GOOGLE_TRENDS"
            selected.append(story)
            selected_event_keys.append((key, signature))
            kept += 1
            if kept == MAX_STORIES_PER_REGION:
                break
        coverage_candidates.sort(
            key=lambda pair: (
                int(pair[1]["independent_publisher_count"]),
                _coverage_profile(pair[0])[0],
                _mba_score(pair[0]),
                pair[0].trend_score,
                max((article.published_at or datetime.min.replace(tzinfo=timezone.utc) for article in pair[0].articles), default=datetime.min.replace(tzinfo=timezone.utc)),
            ),
            reverse=True,
        )
        for story, signal in coverage_candidates:
            if kept >= MAX_STORIES_PER_REGION:
                break
            key = cluster_key(story.articles)
            signature = "\n".join(article.title for article in story.articles)
            if not allow_repeat and state.seen_recently(key, signature=signature, days=repeat_days, now=clock):
                story.freshness_status = "RECENTLY_SELECTED"
                continue
            story.selection_basis = "CROSS_PUBLISHER_COVERAGE"
            story.trend_status = "CONFIRMED_CROSS_PUBLISHER_COVERAGE"
            story.trend_evidence = [signal]
            story.trend_score = round(
                min(100.0, _mba_score(story) * 15 + int(signal["independent_publisher_count"]) * 10 + _coverage_profile(story)[0] * 5 + story.trend_score * 0.25),
                2,
            )
            story.score_components = {
                **story.score_components,
                "independent_publisher_count_24h": float(signal["independent_publisher_count"]),
                "trusted_publisher_domain_count_24h": float(len(signal["publisher_domains"])),
                "cross_publisher_coverage_signal": 1.0,
            }
            selected.append(story)
            selected_event_keys.append((key, signature))
            kept += 1
        if kept < MAX_STORIES_PER_REGION:
            # Public Trends can be dominated by topics outside this audience.
            # Keep an editorially useful daily draft from recent trusted reporting,
            # but expose clearly that it is not Google-Trends-confirmed.
            selected_ids = {story.news_id for story in selected if story.region == region}
            backup_pool = [
                story for story in recent_stories
                if story.news_id not in selected_ids
                and story.trend_status != "CONFIRMED_GOOGLE_TRENDS"
                and _eligible_editorial_backup(story, clock)
                and (
                    allow_repeat
                    or not state.seen_recently(
                        cluster_key(story.articles),
                        signature="\n".join(article.title for article in story.articles),
                        days=repeat_days,
                        now=clock,
                    )
                )
            ]
            backup_pool.sort(
                key=lambda story: (
                    _coverage_profile(story)[1],
                    _coverage_profile(story)[0],
                    _mba_score(story),
                    max((article.published_at or datetime.min.replace(tzinfo=timezone.utc) for article in story.articles), default=datetime.min.replace(tzinfo=timezone.utc)),
                ),
                reverse=True,
            )
            backup_kept = min(len(backup_pool), MAX_STORIES_PER_REGION - kept)
            for story in backup_pool[:backup_kept]:
                key = cluster_key(story.articles)
                signature = "\n".join(article.title for article in story.articles)
                story.selection_basis = "EDITORIAL_BACKUP"
                story.trend_status = "UNCONFIRMED"
                story.trend_evidence = []
                story.verification_reasons.append(
                    "No Google Trends match or three-source coverage signal was available; included as a clearly labeled editor draft, not as trending news"
                )
                selected.append(story)
                selected_event_keys.append((key, signature))
            kept += backup_kept
            fallback_by_region[region] = backup_kept
            if backup_kept:
                warnings.append(
                    f"{region}: generated {backup_kept} editorial backup draft(s); neither Google Trends nor the independent-coverage threshold confirmed them as trending"
                )
        review_drafts_kept = sum(
            story.region == region and story.selection_basis == "GOOGLE_TRENDS" and story.verification_status == "NEEDS_REVIEW"
            for story in selected
        )
        if review_drafts_kept:
            warnings.append(
                f"{region}: included {review_drafts_kept} trend-matched draft(s) from trusted sources for human review; these are not independently corroborated and are not publication-ready"
            )
        coverage_kept = sum(story.region == region and story.selection_basis == "CROSS_PUBLISHER_COVERAGE" for story in selected)
        if coverage_kept:
            warnings.append(
                f"{region}: selected {coverage_kept} story/stories using independent trusted-publisher coverage as a trend proxy; this is not Google search or social-media volume"
            )
        region_diagnostics[region]["selected_for_carousel"] = kept
        if kept < MAX_STORIES_PER_REGION:
            warnings.append(
                f"{region}: {kept} slide(s) selected from {len(regional_clusters)} fresh clusters; "
                f"{len(recent_stories)} passed the recent trusted-source MBA filter, {len(confirmed)} matched Google Trends, "
                f"{len(coverage_candidates)} had three-source coverage, and {fallback_by_region[region]} were editorial backups. "
                "The three-slide regional limit is a maximum, not a target."
            )
    date_dir = output_root / local_date
    date_dir.mkdir(parents=True, exist_ok=True)
    slide_results = []
    for slide_number, story in enumerate(selected, start=1):
        filename = f"slide_{slide_number:02d}_{story.region.lower()}.png"
        path = date_dir / filename
        story.image_path = filename
        story.caption = _caption_section(story)
        layout = render_slide(story, slide_number, len(selected), path)
        with Image.open(path) as image:
            if image.size != (1080, 1350):
                raise ValueError(f"Slide has wrong dimensions: {filename}")
        if layout["text_overflow"]:
            warnings.append(f"Text layout needs inspection on {filename}")
        if layout.get("headline_truncated") or layout.get("summary_truncated") or layout.get("takeaways_truncated"):
            warnings.append(
                f"Slide text was shortened to fit {filename}; the complete headline and summary remain in the workbook and caption"
            )
        slide_results.append(layout)

    caption = make_caption(selected)
    if not selected:
        caption = (
            f"No fresh trusted-source MBA story passed the source, freshness, region, and topic filters for {local_date}. "
            "No carousel slides were generated. Review the region diagnostics and workbook for the candidate shortfall."
        )
    caption_path = date_dir / "carousel_caption.txt"
    caption_path.write_text(caption + "\n", encoding="utf-8")
    workbook_path = date_dir / "Daily_News_Report.xlsx"
    create_workbook(workbook_path, candidates, selected, batch_id, local_date)
    workbook_ok = validate_workbook(workbook_path)

    manifest = {
        "batch_id": batch_id,
        "batch_date_ist": local_date,
        "retrieved_at_utc": clock.isoformat(),
        "candidate_articles_after_dedup": len(articles),
        "fresh_candidate_articles": len(fresh_articles),
        "clusters": [story.to_dict() for story in candidates],
        "selected_news_ids": [story.news_id for story in selected],
        "feed_status": feed_status,
        "google_trends_feed_status": trend_feed_status,
        "google_trend_news_feed_status": trend_news_feed_status,
        "trend_news_articles_collected": len(trend_news_articles),
        "targeted_coverage_feed_status": candidate_coverage_feed_status,
        "targeted_coverage_articles_collected": candidate_coverage_articles_collected,
        "google_trend_signals": [signal.to_dict() for signal in current_trends],
        "ranking": {
            "components": {
                "google_trends_approximate_searches": "lower bound from Google Trends RSS traffic bucket; not an exact count",
                "trend_geographies": "number of configured Google Trends locations reporting the query",
                "headline_match_strength": "transparent token/phrase similarity between source headlines and a fresh Google Trends query or dated Google News RSS coverage for that query; exact related-news URL matches are also accepted",
                "mba_relevance": "deterministic business/economy/policy/geopolitics/healthcare/technology/energy/supply-chain topic match; general-interest items are excluded from slides",
            },
        "minimum_google_trends_searches": min_searches,
        "mba_relevant_candidates": sum(_is_mba_story(story) for story in candidates),
            "matched_by_region": matched_by_region,
            "cross_publisher_coverage_signals_by_region": coverage_by_region,
            "unconfirmed_fallbacks_by_region": fallback_by_region,
            "region_selection_diagnostics": region_diagnostics,
            "maximum_per_region": MAX_STORIES_PER_REGION,
            "selected_by_region": {
                region: sum(story.region == region for story in selected)
                for region in ("INDIA", "WORLD")
            },
            "selection": "separate India and international rankings; prefer fresh same-region Google Trends matches, then stories reported by at least three independent trusted publishers within 24 hours (including targeted Google News RSS coverage searches); fill remaining slots with clearly labeled current trusted-source editor drafts; never exceed three stories per region",
            "trend_score_note": "Google Trends uses its approximate traffic buckets. The alternate coverage proxy counts independent trusted publisher groups in the latest 24 hours; neither signal is a truth score, social-media count, or exact popularity measurement.",
        },
        "selected_stories": [
            {
                "news_id": story.news_id,
                "region": story.region,
                "verification_status": story.verification_status,
                "human_review_required": story.verification_status == "NEEDS_REVIEW" or not _has_confirmed_trend_signal(story),
                "trend_status": story.trend_status,
                "selection_basis": story.selection_basis,
                "trend_evidence": story.trend_evidence,
                "selection_reason": (
                    "No Google Trends match or three-source coverage signal was available. Selected from fresh trusted-source MBA reporting as an editor draft; it is not represented as trending."
                    if story.selection_basis == "EDITORIAL_BACKUP" else
                    f"At least {MIN_INDEPENDENT_PUBLISHERS_FOR_COVERAGE_SIGNAL} independent trusted publishers covered this fresh story within 24 hours. This is a coverage-based trend proxy, not a search-volume or social-media metric."
                    if story.selection_basis == "CROSS_PUBLISHER_COVERAGE" else
                    "Matched a fresh same-region Google Trends signal and passed automated trusted-source checks; an editor must still inspect the source links before manual publication."
                    if story.verification_status == "APPROVED" else
                    "Matched a fresh Google Trends signal and has current trusted-source evidence, but one or more automated checks need review. This is an editor draft, not a truth certification or publication approval."
                ),
            }
            for story in selected
        ],
        "history_persistence": history_persistence,
        "limitations": [
            "Evidence is limited to retrieved RSS metadata and descriptions; article pages are not scraped.",
            "RSS summaries do not establish all underlying facts. Sensitive topics and conflicts require human review.",
            "The publication timestamp is not presented as an event date; event date stays blank unless explicitly supplied by a later verified source.",
            "Google Trends traffic is an approximate lower-bound bucket and reflects the configured country, not total global readership.",
            "Google News RSS results are attributed to the publisher named by the feed; RSS corroboration does not replace reading the linked original reports.",
            "Targeted coverage fallback checks at most six leading MBA-relevant headlines per region (configurable) with a 48-hour Google News RSS query and counts only event-matched, dated, trusted publishers distinct from the original report domains.",
            "A trend signal can come from a fresh same-region Google Trends match or coverage by at least three independent trusted publisher groups within 24 hours. The latter is a publisher-coverage proxy, not search or social-media volume.",
            "When neither trend signal qualifies, fresh trusted-source MBA reports may fill available slots as EDITORIAL_BACKUP drafts; they are explicitly not described as trending.",
            "A NEEDS_REVIEW slide is an editor draft, not a truth certification or publication approval. It may carry unresolved date, conflict, sensitivity, or corroboration flags; open the source pages and resolve those flags before use.",
            "A single curated trusted report can enter the manual editorial queue without requiring a second publisher's repetition. APPROVED means it passed source and automated consistency checks, not that it was independently corroborated or publication-approved.",
        ],
    }
    manifest_path = date_dir / "verification_manifest.json"
    _write_json(manifest_path, manifest)

    if not selected:
        warnings.append("The daily report and ZIP are available, but no fresh trusted-source MBA story passed the source, freshness, region, and topic filters")
    content_paths = [workbook_path, caption_path, manifest_path]
    content_paths.extend(date_dir / str(item["path"]).split("\\")[-1].split("/")[-1] for item in slide_results)
    zip_path = date_dir / "content_package.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in content_paths:
            if path.is_file():
                archive.write(path, arcname=path.name)
    expected_names = {path.name for path in content_paths if path.is_file()}
    with zipfile.ZipFile(zip_path) as archive:
        zip_ok = expected_names.issubset(set(archive.namelist())) and workbook_path.name in archive.namelist()
    summary = _make_summary(
        date_text=local_date, candidates=candidates, selected=selected, slide_results=slide_results,
        workbook_ok=workbook_ok, zip_ok=zip_ok, warnings=warnings, history_persistence=history_persistence,
        text_layout_valid=not any(bool(item.get("text_overflow")) for item in slide_results),
        min_trend_searches=min_searches, matched_by_region=matched_by_region,
        coverage_by_region=coverage_by_region, fallback_by_region=fallback_by_region,
        region_diagnostics=region_diagnostics, trends_collected=len(raw_trends), fresh_trends=len(current_trends),
        trend_news_articles_collected=len(trend_news_articles), trend_news_feed_status=trend_news_feed_status,
        candidate_coverage_feed_status=candidate_coverage_feed_status,
        candidate_coverage_articles_collected=candidate_coverage_articles_collected,
    )
    reason_counts = Counter(reason for story in candidates for reason in story.verification_reasons)
    summary["top_verification_flags"] = [
        {"reason": reason, "story_count": count}
        for reason, count in reason_counts.most_common(8)
    ]
    summary["publisher_feed_status"] = feed_status
    summary["google_trends_feed_status"] = trend_feed_status
    summary["targeted_coverage_feed_status"] = candidate_coverage_feed_status
    summary["google_trends_signal_sample"] = {
        region: {
            "current_signals": sum(signal.region == region for signal in current_trends),
            "signals_at_or_above_volume_floor": sum(
                signal.region == region and (signal.approximate_searches or 0) >= min_searches
                for signal in current_trends
            ),
            "top_topics_by_approximate_searches": [
                {
                    "geo": signal.geo,
                    "query": signal.query,
                    "approx_traffic": signal.approx_traffic,
                    "approximate_searches_lower_bound": signal.approximate_searches,
                    "started_at": signal.started_at.isoformat() if signal.started_at else None,
                    "related_news_items": [
                        {"title": item.get("title", ""), "publisher": item.get("publisher", ""), "url": item.get("url", "")}
                        for item in signal.news_items[:2]
                    ],
                }
                for signal in sorted(
                    (item for item in current_trends if item.region == region),
                    key=lambda item: item.approximate_searches or 0,
                    reverse=True,
                )[:5]
            ],
        }
        for region in ("INDIA", "WORLD")
    }
    _write_json(date_dir / "summary.json", summary)
    if summary["workflow_complete"]:
        for event_key, signature in selected_event_keys:
            state.record(event_key, batch_id, signature=signature, now=clock)
        state.mark_batch_completed(batch_id, summary, now=clock)
    state.close()
    LOGGER.info(
        "Batch %s: %s articles, %s clusters, %s fresh Google Trends signals, %s trend-search articles, %s approved, %s slides (%s)",
        batch_id, len(articles), len(candidates), len(current_trends), len(trend_news_articles), summary["stories_approved"], len(slide_results), summary["trend_selection_mode"],
    )
    return summary
