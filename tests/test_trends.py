from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from insta_post_agent.models import TrendFeed, TrendSignal
from insta_post_agent.trends import (
    apply_trend_evidence,
    fresh_trend_signals,
    match_story_to_trends,
    parse_approximate_searches,
    parse_trend_feed_content,
)
from insta_post_agent.verification import build_story
from test_news_core import NOW, article


def test_google_trends_rss_fixture_parses_volume_and_news_evidence():
    fixture = Path(__file__).parent / "fixtures" / "google_trends_rss.xml"
    feed = TrendFeed("IN", "INDIA", "Google Trends India", "https://trends.google.com/trending/rss?geo=IN")
    signals = parse_trend_feed_content(feed, fixture.read_bytes(), retrieved_at=NOW)

    assert len(signals) == 2
    assert signals[0].query == "FIXTURE ONLY: sample india election update"
    assert signals[0].approx_traffic == "20K+"
    assert signals[0].approximate_searches == 20_000
    assert signals[0].started_at == NOW - timedelta(minutes=15)
    assert signals[0].news_items[0]["publisher"] == "Fixture Publisher"
    assert signals[0].news_items[0]["url"] == "https://fixture.example.test/news/one"
    assert signals[1].approximate_searches == 2_000


def test_search_volume_bucket_parser_uses_lower_bounds():
    assert parse_approximate_searches("1,000+") == 1_000
    assert parse_approximate_searches("1.5M+") == 1_500_000
    assert parse_approximate_searches("20K+") == 20_000
    assert parse_approximate_searches("unknown") is None


def test_trend_match_requires_region_volume_and_headline_evidence():
    story = build_story(
        [article("FIXTURE ONLY: Sample India election update announced", "https://news.example.test/a", "news.example.test", region="INDIA")],
        "fixture-batch",
        now=NOW,
    )
    signal = TrendSignal(
        geo="IN", region="INDIA", query="FIXTURE ONLY: sample india election update", approx_traffic="20K+",
        approximate_searches=20_000, started_at=NOW - timedelta(minutes=15),
        url="https://trends.google.com/trends/explore?q=fixture",
        news_items=[{"title": "FIXTURE ONLY: sample india election update results", "url": "https://fixture.example.test/a", "publisher": "Fixture", "snippet": ""}],
    )

    matches = match_story_to_trends(story, [signal], min_searches=10_000)
    assert len(matches) == 1
    assert match_story_to_trends(story, [signal], min_searches=25_000) == []
    other_region = TrendSignal(**{**signal.__dict__, "geo": "US", "region": "WORLD"})
    assert match_story_to_trends(story, [other_region], min_searches=10_000) == []
    apply_trend_evidence(story, matches)
    assert story.trend_status == "CONFIRMED_GOOGLE_TRENDS"
    assert story.score_components["google_trends_search_volume_lower_bound"] == 20_000


def test_stale_or_undated_trend_signals_are_not_eligible():
    recent = TrendSignal("IN", "INDIA", "recent", "20K+", 20_000, NOW, "https://example.test/recent")
    stale = TrendSignal("IN", "INDIA", "stale", "20K+", 20_000, NOW - timedelta(hours=25), "https://example.test/stale")
    undated = TrendSignal("IN", "INDIA", "undated", "20K+", 20_000, None, "https://example.test/undated")
    fresh, excluded_count = fresh_trend_signals([recent, stale, undated], now=NOW)
    assert fresh == [recent]
    assert excluded_count == 2

