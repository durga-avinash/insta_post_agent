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
from .history import NewsHistory
from .models import Article, Story, TrendSignal
from .render import render_slide
from .scoring import cluster_articles, cluster_key, deduplicate_articles
from .sources import load_sources
from .verification import build_story
from .workbook import create_workbook, validate_workbook
from .trends import (
    apply_trend_evidence,
    collect_trend_news_articles,
    collect_trend_signals,
    fresh_trend_signals,
    load_trend_feeds,
    match_story_to_trends,
)

LOGGER = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
MAX_STORIES_PER_REGION = 3
MAX_SLIDES = 6
DEFAULT_MIN_TREND_SEARCHES = 10_000

_UNSAFE_REVIEW_FLAGS = (
    "does not match its headline",
    "uncertainty/dispute cues",
    "sensitive or developing",
    "numeric details differ",
    "timestamps span more than 24 hours",
    "headline appears to be non-news",
)


def _caption_section(story: Story) -> str:
    return f"{story.headline}\n{story.summary}\nSource: {story.publisher} — {story.original_url}"


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
    """Allow a clearly marked human-review draft only when basic source data exists.

    Single-source items are useful to a human editor, but missing evidence, conflicting
    descriptions, sensitive claims, or uncertain chronology should remain out of slides.
    """
    if story.verification_status != "NEEDS_REVIEW":
        return False
    if not story.original_url.startswith("https://") or not story.publisher.strip() or not story.summary.strip():
        return False
    if not story.articles or not story.evidence:
        return False
    if any(not article.description.strip() or article.published_at is None for article in story.articles):
        return False
    reasons = " ".join(story.verification_reasons).lower()
    return not any(flag in reasons for flag in _UNSAFE_REVIEW_FLAGS)


def _has_recent_report(story: Story, now: datetime, max_age_hours: int = 24) -> bool:
    cutoff = now - timedelta(hours=max_age_hours)
    return any(
        article.published_at is not None
        and cutoff <= article.published_at.astimezone(timezone.utc) <= now + timedelta(minutes=10)
        for article in story.articles
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
    fallback_by_region: dict[str, int],
    trends_collected: int,
    fresh_trends: int,
    trend_news_articles_collected: int,
    trend_news_feed_status: list[dict[str, str]],
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
    return {
        "batch_date": date_text,
        "candidates_collected": sum(len(s.articles) for s in candidates),
        "distinct_story_clusters": len(candidates),
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
        "matched_trends_by_region": matched_by_region,
        "unconfirmed_fallbacks_by_region": fallback_by_region,
        "selected_story_status": [
            {
                "slide_region": story.region,
                "headline": story.headline,
                "verification_status": story.verification_status,
                "trend_status": story.trend_status,
                "trend_evidence": [
                    {
                        "geo": item.get("geo"),
                        "query": item.get("query"),
                        "approx_traffic": item.get("approx_traffic"),
                        "match_method": item.get("match_method"),
                    }
                    for item in story.trend_evidence
                ],
                "human_review_required": story.verification_status == "NEEDS_REVIEW" or story.trend_status != "CONFIRMED_GOOGLE_TRENDS",
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
        "publication_ready": bool(selected) and all(
            s.verification_status == "APPROVED" and s.trend_status == "CONFIRMED_GOOGLE_TRENDS" for s in selected
        ),
        "human_review_required": any(
            s.verification_status == "NEEDS_REVIEW" or s.trend_status == "NOT_CONFIRMED_FALLBACK" for s in selected
        ),
        "trend_selection_mode": (
            "MIXED_TREND_AND_FALLBACK" if any(fallback_by_region.values()) and any(matched_by_region.values())
            else "NEWSROOM_FALLBACK_UNCONFIRMED" if any(fallback_by_region.values())
            else "GOOGLE_TRENDS_MATCHED" if any(matched_by_region.values())
            else "NO_ELIGIBLE_CONTENT"
        ),
        "content_available": bool(selected),
        "batch_status": "READY_FOR_REVIEW" if selected else "NO_ELIGIBLE_CONTENT",
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
            max_trend_queries = int(os.getenv("NEWS_TREND_QUERIES_PER_GEO", "10"))
        except ValueError as exc:
            raise ValueError("NEWS_TREND_QUERIES_PER_GEO must be a positive whole number") from exc
        if max_trend_queries < 1:
            raise ValueError("NEWS_TREND_QUERIES_PER_GEO must be a positive whole number")
        trend_news_articles, trend_news_feed_status = collect_trend_news_articles(
            current_trends,
            min_searches=min_searches,
            max_queries_per_geo=max_trend_queries,
            session=feed_client,
            retrieved_at=clock,
        )
    else:
        # Injected trend fixtures keep unit tests deterministic and network-free.
        trend_news_articles, trend_news_feed_status = [], []
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
        warnings.append("Google Trends was available, but its high-volume topics yielded no usable dated Google News RSS coverage")
    if stale_trend_count:
        warnings.append(f"Ignored {stale_trend_count} undated, stale, or future Google Trends signal(s)")
    failed_trend_feeds = [entry.get("geo", entry.get("source_id", "unknown")) for entry in trend_feed_status if entry.get("status") != "OK"]
    if failed_trend_feeds:
        warnings.append("Google Trends feeds unavailable: " + ", ".join(failed_trend_feeds))
    if not current_trends:
        warnings.append("No fresh Google Trends evidence was obtained; any newsroom fallback story will be explicitly marked NOT CONFIRMED as trending")

    clusters = cluster_articles(fresh_articles)
    candidates = [build_story(group, batch_id, now=clock) for group in clusters if group]
    for story in candidates:
        apply_trend_evidence(story, match_story_to_trends(story, current_trends, min_searches=min_searches))
    candidates.sort(key=lambda story: story.trend_score, reverse=True)

    history_persistence = "local SQLite file; GitHub Actions cache is best-effort and can expire"
    state.prune(now=clock)
    selected: list[Story] = []
    selected_event_keys: list[tuple[str, str]] = []
    matched_by_region: dict[str, int] = {}
    fallback_by_region: dict[str, int] = {}
    for region in ("INDIA", "WORLD"):
        eligible_stories = [
            story for story in candidates
            if story.region == region
            and (story.verification_status == "APPROVED" or _eligible_review_draft(story))
        ]
        eligible_stories = [story for story in eligible_stories if _has_recent_report(story, clock)]
        confirmed = [story for story in eligible_stories if story.trend_status == "CONFIRMED_GOOGLE_TRENDS"]
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
        use_fallback = not confirmed
        pool = eligible_stories if use_fallback else confirmed
        if use_fallback:
            fallback_by_region[region] = 0
        else:
            fallback_by_region[region] = 0
        if use_fallback:
            pool.sort(
                key=lambda story: (
                    story.verification_status == "APPROVED",
                    len({item.independence for item in story.evidence}),
                    story.trend_score,
                    max((article.published_at for article in story.articles if article.published_at), default=clock),
                ),
                reverse=True,
            )
        else:
            pool.sort(
                key=lambda story: (
                    max(int(item["approximate_searches_lower_bound"]) for item in story.trend_evidence),
                    len({str(item["geo"]) for item in story.trend_evidence}),
                    story.verification_status == "APPROVED",
                    story.trend_score,
                ),
                reverse=True,
            )
        kept = 0
        review_selected = 0
        for story in pool:
            key = cluster_key(story.articles)
            signature = "\n".join(article.title for article in story.articles)
            if not allow_repeat and state.seen_recently(key, signature=signature, days=repeat_days, now=clock):
                story.freshness_status = "RECENTLY_SELECTED"
                continue
            if use_fallback:
                story.trend_status = "NOT_CONFIRMED_FALLBACK"
            selected.append(story)
            selected_event_keys.append((key, signature))
            kept += 1
            if use_fallback:
                fallback_by_region[region] += 1
            if story.verification_status == "NEEDS_REVIEW":
                review_selected += 1
            if kept == MAX_STORIES_PER_REGION:
                break
        if kept < MAX_STORIES_PER_REGION:
            if use_fallback and kept:
                warnings.append(f"{region}: selected {kept} safe newsroom fallback story/stories; Google Trends did not confirm any eligible story in this region")
            elif not kept:
                warnings.append(f"{region}: no eligible story was available after evidence and freshness checks")
            else:
                warnings.append(f"{region}: selected {kept} Google Trends-matched story/stories; fewer than {MAX_STORIES_PER_REGION} met the evidence and high-trend requirements")
        if review_selected:
            warnings.append(f"{region}: {review_selected} selected slide(s) are NEEDS_REVIEW and require human corroboration before publication")
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
        if layout.get("headline_truncated") or layout.get("summary_truncated"):
            warnings.append(
                f"Slide text was shortened to fit {filename}; the complete headline and summary remain in the workbook and caption"
            )
        slide_results.append(layout)

    caption = make_caption(selected)
    if not selected:
        caption = "No safe fresh story was available for today's Google Trends match or newsroom fallback. Please review the verification manifest."
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
        "google_trend_signals": [signal.to_dict() for signal in current_trends],
        "ranking": {
            "components": {
                "google_trends_approximate_searches": "lower bound from Google Trends RSS traffic bucket; not an exact count",
                "trend_geographies": "number of configured Google Trends locations reporting the query",
                "headline_match_strength": "transparent token/phrase similarity between source headlines and a fresh Google Trends query or dated Google News RSS coverage for that query; exact related-news URL matches are also accepted",
            },
            "minimum_google_trends_searches": min_searches,
            "matched_by_region": matched_by_region,
            "unconfirmed_fallbacks_by_region": fallback_by_region,
            "maximum_per_region": MAX_STORIES_PER_REGION,
            "selected_by_region": {
                region: sum(story.region == region for story in selected)
                for region in ("INDIA", "WORLD")
            },
            "selection": "separate India and international rankings; collect dated Google News RSS coverage for the highest-volume fresh Google Trends topics, require a same-region headline or exact related-news URL match and the configured approximate-search floor; use a clearly marked source-backed newsroom fallback only in a region with no qualifying trend match; never exceed three per region",
            "trend_score_note": "Trend scores are internal ranking indicators derived from Google Trends approximate traffic buckets and match strength; only the raw traffic bucket is Google-provided, and its count is approximate.",
        },
        "selected_stories": [
            {
                "news_id": story.news_id,
                "region": story.region,
                "verification_status": story.verification_status,
                "human_review_required": story.verification_status == "NEEDS_REVIEW",
                "trend_status": story.trend_status,
                "trend_evidence": story.trend_evidence,
                "selection_reason": (
                    "No eligible high-trend match was available in this region; this source-backed item is only a newsroom fallback and must not be described as trending."
                    if story.trend_status == "NOT_CONFIRMED_FALLBACK"
                    else "Matched a fresh Google Trends signal; the verification status above still controls whether human review is required."
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
            "A newsroom fallback is not search-trend verified and must not be labeled trending; a human editor must check the original reports before publication.",
        ],
    }
    manifest_path = date_dir / "verification_manifest.json"
    _write_json(manifest_path, manifest)

    if not selected:
        warnings.append("No safe, fresh source-backed story was available for either the Google Trends selection or newsroom fallback")
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
        fallback_by_region=fallback_by_region, trends_collected=len(raw_trends), fresh_trends=len(current_trends),
        trend_news_articles_collected=len(trend_news_articles), trend_news_feed_status=trend_news_feed_status,
    )
    reason_counts = Counter(reason for story in candidates for reason in story.verification_reasons)
    summary["top_verification_flags"] = [
        {"reason": reason, "story_count": count}
        for reason, count in reason_counts.most_common(8)
    ]
    summary["publisher_feed_status"] = feed_status
    summary["google_trends_feed_status"] = trend_feed_status
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

