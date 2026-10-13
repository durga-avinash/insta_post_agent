from __future__ import annotations

import json
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from insta_post_agent.caption import make_caption
from insta_post_agent.history import NewsHistory
from insta_post_agent.models import TrendSignal
from insta_post_agent.pipeline import run_daily
from insta_post_agent.pipeline import _best_unused_trend_evidence, _eligible_review_draft
from insta_post_agent.render import render_slide
from insta_post_agent.verification import build_story
from insta_post_agent.workbook import create_workbook, validate_workbook

from test_news_core import NOW, article


def fixture_story(title: str, region: str, slug: str, now=NOW):
    first = article(
        title, f"https://bbc.com/fixture/{slug}", "bbc.com",
        description=f"FIXTURE ONLY: A synthetic classroom report about {slug.replace('-', ' ')} offers context for testing.",
        region=region, published_at=now - timedelta(minutes=12), source_id="desk-a",
    )
    second = article(
        title.replace("FIXTURE ONLY:", "FIXTURE ONLY: New details on"), f"https://reuters.com/fixture/{slug}", "reuters.com",
        description=f"FIXTURE ONLY: A separate imaginary desk describes {slug.replace('-', ' ')} in a test fixture.",
        region=region, published_at=now - timedelta(minutes=9), source_id="desk-b",
    )
    second.category = first.category
    return build_story([first, second], "fixture-batch", now=now)


def fixture_trend_signals(stories):
    return [
        TrendSignal(
            geo="IN" if story.region == "INDIA" else "US",
            region=story.region,
            query=story.headline,
            approx_traffic="50K+",
            approximate_searches=50_000,
            started_at=NOW - timedelta(minutes=5),
            url="https://trends.google.com/trends/explore?q=fixture",
            news_items=[],
        )
        for story in stories
    ]


def test_slide_dimensions_layout_workbook_and_caption_links(tmp_path):
    story = fixture_story("FIXTURE ONLY: Sample science teams share a classroom research note", "WORLD", "science-classroom")
    image_path = tmp_path / "slide.png"
    layout = render_slide(story, 1, 6, image_path)
    with Image.open(image_path) as image:
        assert image.size == (1080, 1350)
    assert layout["text_overflow"] is False
    story.caption = f"{story.headline} {story.original_url}"
    caption = make_caption([story])
    assert story.original_url in caption
    assert "1. [APPROVED] FIXTURE ONLY" in caption
    workbook_path = tmp_path / "Daily_News_Report.xlsx"
    create_workbook(workbook_path, [story], [story], "fixture-batch", "2026-10-13")
    assert validate_workbook(workbook_path)


def test_long_slide_copy_is_wrapped_and_shortened_without_canvas_overflow(tmp_path):
    story = fixture_story("FIXTURE ONLY: Sample science teams share a classroom research note", "WORLD", "long-copy")
    story.headline = "FIXTURE ONLY: " + "internationally coordinated research teams publish a detailed classroom science report " * 8
    story.summary = "A synthetic test description with unusually long copy and a deliberately unbroken token " + ("internationalization" * 18)
    image_path = tmp_path / "long-copy-slide.png"

    layout = render_slide(story, 4, 6, image_path)

    with Image.open(image_path) as image:
        assert image.size == (1080, 1350)
    assert layout["text_overflow"] is False
    assert layout["headline_truncated"] is True
    assert layout["summary_truncated"] is True


def test_full_fixture_pipeline_writes_six_slides_and_complete_zip(tmp_path, monkeypatch):
    titles = [
        ("INDIA", "FIXTURE ONLY: Kerala districts add clean water monitoring sensors", "water-sensors"),
        ("INDIA", "FIXTURE ONLY: Rural clinics test mobile appointment scheduling", "clinic-scheduling"),
        ("INDIA", "FIXTURE ONLY: Ladakh solar researchers start new training centre", "solar-training"),
        ("WORLD", "FIXTURE ONLY: Coastal cities publish a flood readiness map", "coastal-flood-map"),
        ("WORLD", "FIXTURE ONLY: Island schools study renewable energy methods", "island-energy"),
        ("WORLD", "FIXTURE ONLY: Ocean students chart coral reef research findings", "reef-research"),
    ]
    expected_now = NOW
    records = [fixture_story(title, region, slug, now=expected_now) for region, title, slug in titles]
    raw_articles = [article for story in records for article in story.articles]
    monkeypatch.setattr(
        "insta_post_agent.pipeline.collect_sources",
        lambda *args, **kwargs: (raw_articles, [{"source_id": "fixture", "status": "OK", "items": str(len(raw_articles))}]),
    )
    summary = run_daily(
        root=Path.cwd(), output_root=tmp_path / "output", history_path=tmp_path / "state" / "history.sqlite3",
        now=expected_now, sources=[], trend_signals=fixture_trend_signals(records),
    )
    # Batch date is computed in IST; the fixture timestamp is 09:00 IST.
    date_dir = tmp_path / "output" / "2026-10-13"
    assert summary["workflow_complete"] is True
    assert summary["images_generated"] == 6
    assert summary["max_stories_per_region"] == 3
    assert summary["slide_limit_respected"] is True
    assert summary["all_selected_slides_generated"] is True
    assert validate_workbook(date_dir / "Daily_News_Report.xlsx")
    with zipfile.ZipFile(date_dir / "content_package.zip") as archive:
        names = set(archive.namelist())
        assert "Daily_News_Report.xlsx" in names
        assert "carousel_caption.txt" in names
        assert "verification_manifest.json" in names
        assert sum(name.endswith(".png") for name in names) == 6
    manifest = json.loads((date_dir / "verification_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["selected_news_ids"]) == 6
    duplicate = run_daily(
        root=Path.cwd(), output_root=tmp_path / "output", history_path=tmp_path / "state" / "history.sqlite3",
        now=expected_now, sources=[], trend_signals=fixture_trend_signals(records), allow_repeat_batch=False,
    )
    assert duplicate["duplicate_batch"] is True
    assert duplicate["workflow_complete"] is True

    repeated = run_daily(
        root=Path.cwd(), output_root=tmp_path / "repeat-output", history_path=tmp_path / "state" / "history.sqlite3",
        now=expected_now, sources=[], trend_signals=fixture_trend_signals(records), allow_repeat_batch=True,
    )
    assert repeated.get("duplicate_batch", False) is False
    assert repeated["images_generated"] == 6


def test_pipeline_completes_with_only_the_high_trend_stories_available(tmp_path, monkeypatch):
    titles = [
        ("INDIA", "FIXTURE ONLY: Kerala districts add clean water monitoring sensors", "water-sensors"),
        ("INDIA", "FIXTURE ONLY: Rural clinics test mobile appointment scheduling", "clinic-scheduling"),
        ("WORLD", "FIXTURE ONLY: Coastal cities publish a flood readiness map", "coastal-flood-map"),
    ]
    records = [fixture_story(title, region, slug, now=NOW) for region, title, slug in titles]
    raw_articles = [article for story in records for article in story.articles]
    monkeypatch.setattr(
        "insta_post_agent.pipeline.collect_sources",
        lambda *args, **kwargs: (raw_articles, [{"source_id": "fixture", "status": "OK", "items": str(len(raw_articles))}]),
    )

    summary = run_daily(
        root=Path.cwd(), output_root=tmp_path / "output", history_path=tmp_path / "state" / "history.sqlite3",
        now=NOW, sources=[], trend_signals=fixture_trend_signals(records),
    )
    date_dir = tmp_path / "output" / "2026-10-13"

    assert summary["workflow_complete"] is True
    assert summary["batch_status"] == "READY_FOR_REVIEW"
    assert summary["images_generated"] == 3
    assert summary["selected_by_region"] == {"INDIA": 2, "WORLD": 1}
    assert summary["publication_ready"] is True
    with zipfile.ZipFile(date_dir / "content_package.zip") as archive:
        names = set(archive.namelist())
        assert sum(name.endswith(".png") for name in names) == 3
        assert "Daily_News_Report.xlsx" in names
        assert "carousel_caption.txt" in names
        assert "verification_manifest.json" in names


def test_current_trusted_single_source_becomes_marked_review_draft(tmp_path, monkeypatch):
    report = article(
        "FIXTURE ONLY: Indian city opens transit station after repairs",
        "https://www.bbc.com/news/fixture-transit-station",
        "bbc.com",
        description="A fixture report says an Indian city opens a transit station after repairs.",
        region="INDIA",
        published_at=NOW - timedelta(minutes=20),
        source_id="bbc-india-fixture",
    )
    monkeypatch.setattr(
        "insta_post_agent.pipeline.collect_sources",
        lambda *args, **kwargs: ([report], [{"source_id": "bbc-fixture", "status": "OK", "items": "1"}]),
    )
    signal = TrendSignal(
        geo="IN",
        region="INDIA",
        query=report.title,
        approx_traffic="50K+",
        approximate_searches=50_000,
        started_at=NOW - timedelta(minutes=5),
        url="https://trends.google.com/trends/explore?q=fixture",
        news_items=[],
    )

    summary = run_daily(
        root=Path.cwd(),
        output_root=tmp_path / "output",
        history_path=tmp_path / "state" / "history.sqlite3",
        now=NOW,
        sources=[],
        trend_signals=[signal],
    )
    date_dir = tmp_path / "output" / "2026-10-13"

    assert _eligible_review_draft(build_story([report], "fixture-batch", now=NOW)) is True
    assert summary["workflow_complete"] is True
    assert summary["batch_status"] == "REVIEW_DRAFT_AVAILABLE"
    assert summary["trend_selection_mode"] == "TRENDS_MATCHED_REVIEW_DRAFT"
    assert summary["selected_by_region"] == {"INDIA": 1, "WORLD": 0}
    assert summary["stories_selected_needs_review"] == 1
    assert summary["publication_ready"] is False
    assert summary["images_generated"] == 1
    with zipfile.ZipFile(date_dir / "content_package.zip") as archive:
        names = set(archive.namelist())
        assert "slide_01_india.png" in names
        assert "verification_manifest.json" in names
    caption = (date_dir / "carousel_caption.txt").read_text(encoding="utf-8")
    assert "Review draft" in caption
    assert "NEEDS_REVIEW" in caption
    manifest = json.loads((date_dir / "verification_manifest.json").read_text(encoding="utf-8"))
    assert manifest["selected_stories"][0]["verification_status"] == "NEEDS_REVIEW"
    assert "independent corroboration is missing" in manifest["selected_stories"][0]["selection_reason"]


def test_review_drafts_exclude_unknown_publishers_and_sensitive_topics():
    unknown = article(
        "FIXTURE ONLY: Local city opens a new commuter station",
        "https://unknown.example.test/station",
        "unknown.example.test",
        description="A fixture report says a local city opens a new commuter station.",
    )
    sensitive = article(
        "FIXTURE ONLY: City reports casualties after transit station attack",
        "https://www.bbc.com/news/fixture-sensitive",
        "bbc.com",
        description="A fixture report says a city reports casualties after a transit station attack.",
    )

    assert _eligible_review_draft(build_story([unknown], "fixture-batch", now=NOW)) is False
    assert _eligible_review_draft(build_story([sensitive], "fixture-batch", now=NOW)) is False


def test_pipeline_never_selects_more_than_three_per_region(tmp_path, monkeypatch):
    titles = [
        ("INDIA", "FIXTURE ONLY: Himalayan rail tunnel opens to local commuters", "himalayan-rail"),
        ("INDIA", "FIXTURE ONLY: Coastal hospital launches mobile clinic vessel", "coastal-clinic"),
        ("INDIA", "FIXTURE ONLY: Desert solar grid adds battery storage site", "desert-solar"),
        ("INDIA", "FIXTURE ONLY: Bengaluru libraries digitize Kannada manuscripts", "bengaluru-library"),
        ("WORLD", "FIXTURE ONLY: Arctic observatories chart seasonal ice retreat", "arctic-ice"),
        ("WORLD", "FIXTURE ONLY: Pacific islands restore native mangrove forests", "pacific-mangroves"),
        ("WORLD", "FIXTURE ONLY: European laboratories map rare protein structure", "european-protein"),
        ("WORLD", "FIXTURE ONLY: African cities extend cross-border rail service", "african-rail"),
    ]
    records = [fixture_story(title, region, slug, now=NOW) for region, title, slug in titles]
    raw_articles = [article for story in records for article in story.articles]
    monkeypatch.setattr(
        "insta_post_agent.pipeline.collect_sources",
        lambda *args, **kwargs: (raw_articles, [{"source_id": "fixture", "status": "OK", "items": str(len(raw_articles))}]),
    )

    summary = run_daily(
        root=Path.cwd(), output_root=tmp_path / "output", history_path=tmp_path / "state" / "history.sqlite3",
        now=NOW, sources=[], trend_signals=fixture_trend_signals(records),
    )

    assert summary["workflow_complete"] is True
    assert summary["matched_trends_by_region"] == {"INDIA": 4, "WORLD": 4}
    assert summary["selected_by_region"] == {"INDIA": 3, "WORLD": 3}
    assert summary["images_generated"] == 6
    assert summary["slide_limit_respected"] is True


def test_no_trend_match_never_places_a_fallback_story_in_the_carousel(tmp_path, monkeypatch):
    records = [
        article(
            "FIXTURE ONLY: Local library opens a community reading room",
            "https://single-source.example.test/library",
            "single-source.example.test",
            description="A fixture report says a local library opens a community reading room.",
            region="INDIA",
        ),
        article(
            "FIXTURE ONLY: Island museum starts public archive project",
            "https://world-source.example.test/archive",
            "world-source.example.test",
            description="A fixture report says an island museum starts a public archive project.",
            region="WORLD",
        ),
    ]
    monkeypatch.setattr(
        "insta_post_agent.pipeline.collect_sources",
        lambda *args, **kwargs: (records, [{"source_id": "fixture", "status": "OK", "items": "2"}]),
    )

    summary = run_daily(
        root=Path.cwd(), output_root=tmp_path / "output", history_path=tmp_path / "state" / "history.sqlite3",
        now=NOW, sources=[], trend_signals=[],
    )
    date_dir = tmp_path / "output" / "2026-10-13"

    assert summary["workflow_complete"] is True
    assert summary["batch_status"] == "NO_PUBLISHABLE_STORIES"
    assert summary["trend_selection_mode"] == "NO_ELIGIBLE_CONTENT"
    assert summary["images_generated"] == 0
    assert summary["selected_by_region"] == {"INDIA": 0, "WORLD": 0}
    assert summary["unconfirmed_fallbacks_by_region"] == {"INDIA": 0, "WORLD": 0}
    assert summary["publication_ready"] is False
    assert summary["zip_created"] is True
    with zipfile.ZipFile(date_dir / "content_package.zip") as archive:
        names = set(archive.namelist())
        assert "Daily_News_Report.xlsx" in names
        assert "carousel_caption.txt" in names
        assert "verification_manifest.json" in names
        assert sum(name.endswith(".png") for name in names) == 0
    assert "No story passed today's Google Trends and trusted-source checks" in (date_dir / "carousel_caption.txt").read_text(encoding="utf-8")


def test_trend_volume_threshold_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="positive whole number"):
        run_daily(
            root=Path.cwd(), output_root=tmp_path / "output", history_path=tmp_path / "state" / "history.sqlite3",
            now=NOW, sources=[], min_trend_searches=0, trend_signals=[],
        )


def test_recent_event_history_prevents_duplicate_daily_selection(tmp_path):
    path = tmp_path / "history.sqlite3"
    key = "fixture-event-cluster"
    state = NewsHistory(path)
    signature = "Fixture river sensors added across sample districts\nFixture sample districts add river sensors"
    state.record(key, "fixture-batch", signature=signature, now=NOW)
    assert state.seen_recently(key, signature=signature, now=NOW)
    changed_headline = "Fixture new river sensors extend across sample districts"
    assert state.seen_recently("another-key", signature=changed_headline, now=NOW + timedelta(days=1))
    assert not state.seen_recently(key, days=2, now=NOW + timedelta(days=3))
    state.mark_batch_completed("fixture-batch", {"workflow_complete": True}, now=NOW)
    assert state.completed_batch("fixture-batch")["workflow_complete"] is True
    state.close()


def test_pipeline_keeps_unverified_untrended_stories_out_of_carousel(tmp_path, monkeypatch):
    fixtures = [
        ("INDIA", "FIXTURE ONLY: City library opens community reading room", "A city library opens a community reading room today for local readers."),
        ("INDIA", "FIXTURE ONLY: Rural clinics pilot appointment calendar", "Rural clinics pilot an appointment calendar today for local visits."),
        ("INDIA", "FIXTURE ONLY: Solar lab shares battery efficiency report", "A solar lab shares a battery efficiency report with local researchers."),
        ("WORLD", "FIXTURE ONLY: Coastal schools publish flood learning map", "Coastal schools publish a flood learning map to explain local planning."),
        ("WORLD", "FIXTURE ONLY: Museum archives start public digitization program", "Museum archives start a public digitization program for historic records."),
        ("WORLD", "FIXTURE ONLY: Island transit tests electric ferry timetable", "Island transit tests an electric ferry timetable for local commuters."),
    ]
    records = [
        article(
            title,
            f"https://single-source-{index}.example.test/{index}",
            f"single-source-{index}.example.test",
            description=description,
            region=region,
            source_id=f"single-source-{index}",
        )
        for index, (region, title, description) in enumerate(fixtures)
    ]
    monkeypatch.setattr(
        "insta_post_agent.pipeline.collect_sources",
        lambda *args, **kwargs: (records, [{"source_id": "fixture", "status": "OK", "items": str(len(records))}]),
    )
    summary = run_daily(
        root=Path.cwd(),
        output_root=tmp_path / "output",
        history_path=tmp_path / "state" / "history.sqlite3",
        now=NOW,
        sources=[],
        trend_signals=[],
    )
    date_dir = tmp_path / "output" / "2026-10-13"
    assert summary["workflow_complete"] is True
    assert summary["images_generated"] == 0
    assert summary["stories_approved"] == 0
    assert summary["stories_selected_needs_review"] == 0
    assert summary["human_review_required"] is False
    assert summary["publication_ready"] is False
    assert summary["trend_selection_mode"] == "NO_ELIGIBLE_CONTENT"
    assert summary["unconfirmed_fallbacks_by_region"] == {"INDIA": 0, "WORLD": 0}
    assert summary["excel_validated"] is True
    assert sum(1 for image in date_dir.glob("slide_*.png")) == 0
    with zipfile.ZipFile(date_dir / "content_package.zip") as archive:
        names = set(archive.namelist())
        assert "Daily_News_Report.xlsx" in names
        assert "carousel_caption.txt" in names
        assert "verification_manifest.json" in names
        assert sum(name.endswith(".png") for name in names) == 0
    caption = (date_dir / "carousel_caption.txt").read_text(encoding="utf-8")
    assert "No story passed today's Google Trends and trusted-source checks" in caption
    manifest = json.loads((date_dir / "verification_manifest.json").read_text(encoding="utf-8"))
    assert manifest["selected_stories"] == []


def test_caption_keeps_six_story_urls_under_target_length():
    stories = [fixture_story(f"FIXTURE ONLY: headline number {n} has a short synthetic description", "WORLD", f"story-{n}") for n in range(1, 7)]
    for index, story in enumerate(stories):
        story.original_url = f"https://fixture-source.example.test/news/story-{index}?original=1"
    caption = make_caption(stories)
    assert len(caption) <= 2200
    assert all(story.original_url in caption for story in stories)


def test_trend_topic_diversity_uses_next_unused_topic():
    story = fixture_story("FIXTURE ONLY: Man United publishes a sample match report", "WORLD", "topic-diversity")
    story.trend_evidence = [
        {"query": "Man Utd", "approximate_searches_lower_bound": 2_000, "match_strength": 0.9},
        {"query": "Championship", "approximate_searches_lower_bound": 1_000, "match_strength": 0.8},
    ]

    first = _best_unused_trend_evidence(story, set())
    second = _best_unused_trend_evidence(story, {"man utd"})

    assert first["query"] == "Man Utd"
    assert second["query"] == "Championship"

