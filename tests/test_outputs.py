from __future__ import annotations

import json
import zipfile
from datetime import timedelta
from pathlib import Path

from PIL import Image

from insta_post_agent.caption import make_caption
from insta_post_agent.history import NewsHistory
from insta_post_agent.pipeline import run_daily
from insta_post_agent.render import render_slide
from insta_post_agent.verification import build_story
from insta_post_agent.workbook import create_workbook, validate_workbook

from test_news_core import NOW, article


def fixture_story(title: str, region: str, slug: str, now=NOW):
    first = article(
        title, f"https://desk-a.example.test/{slug}", "desk-a.example.test",
        description=f"FIXTURE ONLY: A synthetic classroom report about {slug.replace('-', ' ')} offers context for testing.",
        region=region, published_at=now - timedelta(minutes=12), source_id="desk-a",
    )
    second = article(
        title.replace("FIXTURE ONLY:", "FIXTURE ONLY: New details on"), f"https://desk-b.example.test/{slug}", "desk-b.example.test",
        description=f"FIXTURE ONLY: A separate imaginary desk describes {slug.replace('-', ' ')} in a test fixture.",
        region=region, published_at=now - timedelta(minutes=9), source_id="desk-b",
    )
    second.category = first.category
    return build_story([first, second], "fixture-batch", now=now)


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
        now=expected_now, sources=[],
    )
    # Batch date is computed in IST; the fixture timestamp is 09:00 IST.
    date_dir = tmp_path / "output" / "2026-10-13"
    assert summary["workflow_complete"] is True
    assert summary["images_generated"] == 6
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
        now=expected_now, sources=[],
    )
    assert duplicate["duplicate_batch"] is True
    assert duplicate["workflow_complete"] is False


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


def test_pipeline_creates_honestly_labeled_review_drafts_when_only_single_sources_exist(tmp_path, monkeypatch):
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
    )
    date_dir = tmp_path / "output" / "2026-10-13"
    assert summary["workflow_complete"] is True
    assert summary["images_generated"] == 6
    assert summary["stories_approved"] == 0
    assert summary["stories_selected_needs_review"] == 6
    assert summary["human_review_required"] is True
    assert summary["publication_ready"] is False
    assert summary["excel_validated"] is True
    assert sum(1 for image in date_dir.glob("slide_*.png")) == 6
    with zipfile.ZipFile(date_dir / "content_package.zip") as archive:
        names = set(archive.namelist())
        assert "Daily_News_Report.xlsx" in names
        assert "carousel_caption.txt" in names
        assert "verification_manifest.json" in names
        assert sum(name.endswith(".png") for name in names) == 6
    caption = (date_dir / "carousel_caption.txt").read_text(encoding="utf-8")
    assert "Review draft" in caption
    assert caption.count("[NEEDS_REVIEW]") == 6
    manifest = json.loads((date_dir / "verification_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["selected_stories"]) == 6
    assert all(story["human_review_required"] for story in manifest["selected_stories"])


def test_caption_keeps_six_story_urls_under_target_length():
    stories = [fixture_story(f"FIXTURE ONLY: headline number {n} has a short synthetic description", "WORLD", f"story-{n}") for n in range(1, 7)]
    for index, story in enumerate(stories):
        story.original_url = f"https://fixture-source.example.test/news/story-{index}?original=1"
    caption = make_caption(stories)
    assert len(caption) <= 2200
    assert all(story.original_url in caption for story in stories)

