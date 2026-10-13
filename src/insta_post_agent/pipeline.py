from __future__ import annotations

import json
import logging
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from .caption import make_caption
from .collector import collect_sources
from .history import NewsHistory
from .models import Article, Story
from .render import render_slide
from .scoring import cluster_articles, cluster_key, deduplicate_articles
from .sources import load_sources
from .verification import build_story
from .workbook import create_workbook, validate_workbook

LOGGER = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

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
) -> dict[str, object]:
    slide_count = len(slide_results)
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
        "all_six_slides_generated": slide_count == 6,
        "excel_validated": workbook_ok,
        "images_layout_valid": text_layout_valid,
        "zip_created": zip_ok,
        "publication_ready": slide_count == 6 and all(s.verification_status == "APPROVED" for s in selected),
        "human_review_required": any(s.verification_status == "NEEDS_REVIEW" for s in selected),
        "workflow_complete": slide_count == 6 and workbook_ok and zip_ok and text_layout_valid,
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
) -> dict[str, object]:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_date = clock.astimezone(IST).date().isoformat()
    batch_id = local_date.replace("-", "")
    max_age = max_age_hours if max_age_hours is not None else int(os.getenv("NEWS_MAX_AGE_HOURS", "36"))
    repeat_days = history_days if history_days is not None else int(os.getenv("NEWS_HISTORY_DAYS", "2"))
    per_feed = max_feed_items if max_feed_items is not None else int(os.getenv("NEWS_MAX_FEED_ITEMS", "40"))
    state = NewsHistory(history_path)
    prior_batch = state.completed_batch(batch_id)
    if prior_batch is not None:
        state.close()
        return {
            **prior_batch,
            "workflow_complete": False,
            "duplicate_batch": True,
            "warnings": [f"Batch {batch_id} already completed; duplicate daily output was suppressed. Download the earlier run artifact."],
        }
    sources = sources if sources is not None else load_sources(sources_path or root / "config" / "sources.json")
    raw_articles, feed_status = collect_sources(sources, max_items=per_feed, session=feed_client)
    articles = deduplicate_articles(raw_articles)
    fresh_articles, stale_count, future_count = _fresh_articles(articles, clock, max_age)
    warnings: list[str] = []
    if stale_count:
        warnings.append(f"Ignored {stale_count} candidate articles older than the {max_age}-hour freshness window")
    if future_count:
        warnings.append(f"Ignored {future_count} articles with publication timestamps more than 10 minutes in the future")
    failed_feeds = [entry["source_id"] for entry in feed_status if entry["status"] != "OK"]
    if failed_feeds:
        warnings.append("Feeds unavailable: " + ", ".join(failed_feeds))
    if not fresh_articles:
        warnings.append("No recent source items were available; no story content was invented")

    clusters = cluster_articles(fresh_articles)
    candidates = [build_story(group, batch_id, now=clock) for group in clusters if group]
    candidates.sort(key=lambda story: story.trend_score, reverse=True)

    history_persistence = "local SQLite file; GitHub Actions cache is best-effort and can expire"
    state.prune(now=clock)
    selected: list[Story] = []
    selected_event_keys: list[tuple[str, str]] = []
    for region in ("INDIA", "WORLD"):
        region_stories = [s for s in candidates if s.region == region and s.verification_status == "APPROVED"]
        review_drafts = [s for s in candidates if s.region == region and _eligible_review_draft(s)]
        kept = 0
        review_selected = 0
        # Fully approved items are preferred. If there are not enough, include only
        # reviewable source-backed drafts, keeping NEEDS_REVIEW explicit in every output.
        for story in [*region_stories, *review_drafts]:
            key = cluster_key(story.articles)
            signature = "\n".join(article.title for article in story.articles)
            if state.seen_recently(key, signature=signature, days=repeat_days, now=clock):
                story.freshness_status = "RECENTLY_SELECTED"
                continue
            selected.append(story)
            selected_event_keys.append((key, signature))
            kept += 1
            if story.verification_status == "NEEDS_REVIEW":
                review_selected += 1
            if kept == 3:
                break
        if kept < 3:
            warnings.append(f"{region}: only {kept} of 3 slide slots had sufficient source material after verification and freshness checks")
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
        layout = render_slide(story, slide_number, 6, path)
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
        caption = "Today's evidence-backed brief has no stories ready for publication. Please review the verification manifest."
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
        "ranking": {
            "components": {"recency": "exponential decay over age in hours", "independent_coverage_proxy": "20 points per additional distinct source domain (capped at 40)", "public_significance_proxy": "transparent keyword proxy (capped at 25)", "search_interest": "not available; scored as 0 and never fabricated"},
            "selection": "separate region rankings; APPROVED stories first, then eligible source-backed NEEDS_REVIEW drafts with visible warnings; up to three per region; recent events skipped",
        },
        "selected_stories": [
            {
                "news_id": story.news_id,
                "region": story.region,
                "verification_status": story.verification_status,
                "human_review_required": story.verification_status == "NEEDS_REVIEW",
                "selection_reason": (
                    "No APPROVED item filled this region's remaining slot; basic source metadata is present, but independent corroboration is still required."
                    if story.verification_status == "NEEDS_REVIEW"
                    else "Passed the automated editorial checks."
                ),
            }
            for story in selected
        ],
        "history_persistence": history_persistence,
        "limitations": [
            "Evidence is limited to retrieved RSS metadata and descriptions; article pages are not scraped.",
            "RSS summaries do not establish all underlying facts. Sensitive topics and conflicts require human review.",
            "The publication timestamp is not presented as an event date; event date stays blank unless explicitly supplied by a later verified source.",
        ],
    }
    manifest_path = date_dir / "verification_manifest.json"
    _write_json(manifest_path, manifest)

    if len(selected) < 6:
        warnings.append(f"Only {len(selected)} evidence-approved slides were generated; six are required for a complete batch")
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
    )
    _write_json(date_dir / "summary.json", summary)
    if summary["workflow_complete"]:
        for event_key, signature in selected_event_keys:
            state.record(event_key, batch_id, signature=signature, now=clock)
        state.mark_batch_completed(batch_id, summary, now=clock)
    state.close()
    LOGGER.info("Batch %s: %s articles, %s clusters, %s approved, %s slides", batch_id, len(articles), len(candidates), summary["stories_approved"], len(slide_results))
    return summary

