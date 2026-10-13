from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from insta_post_agent.models import TrendFeed, TrendSignal
from insta_post_agent.trends import (
    apply_trend_evidence,
    collect_trend_news_articles,
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


def test_trend_related_news_canonical_url_confirms_rewritten_headline():
    linked_url = "https://news.example.test/story/kerala-water?utm_source=rss"
    story = build_story(
        [article(
            "FIXTURE ONLY: Kerala announces a new water safety programme",
            "https://news.example.test/story/kerala-water?utm_medium=feed",
            "news.example.test",
            region="INDIA",
        )],
        "fixture-batch",
        now=NOW,
    )
    signal = TrendSignal(
        geo="IN", region="INDIA", query="FIXTURE ONLY: potable water kerala", approx_traffic="100K+",
        approximate_searches=100_000, started_at=NOW - timedelta(minutes=8),
        url="https://trends.google.com/trending/rss?geo=IN",
        news_items=[{"title": "FIXTURE ONLY: State water safety programme", "url": linked_url, "publisher": "Fixture publisher", "snippet": ""}],
    )

    matches = match_story_to_trends(story, [signal], min_searches=10_000)

    assert len(matches) == 1
    assert matches[0]["match_strength"] == 1.0
    assert matches[0]["match_method"] == "google_trends_related_news_url"
    assert matches[0]["matched_news_item_url"] == "https://news.example.test/story/kerala-water"


def test_generic_single_word_trend_cannot_match_by_keyword_alone():
    story = build_story(
        [article("FIXTURE ONLY: Local championship results announced", "https://news.example.test/a", "news.example.test", region="WORLD")],
        "fixture-batch",
        now=NOW,
    )
    signal = TrendSignal(
        geo="US", region="WORLD", query="championship", approx_traffic="20K+",
        approximate_searches=20_000, started_at=NOW - timedelta(minutes=3),
        url="https://trends.google.com/trends/explore?q=championship",
    )

    assert match_story_to_trends(story, [signal], min_searches=10_000) == []


def test_trend_match_needs_most_query_terms_not_one_common_word():
    story = build_story(
        [article("FIXTURE ONLY: Manchester United prepares for a sample fixture", "https://news.example.test/a", "news.example.test", region="WORLD")],
        "fixture-batch",
        now=NOW,
    )
    signal = TrendSignal(
        geo="US", region="WORLD", query="Manchester United transfer deadline", approx_traffic="20K+",
        approximate_searches=20_000, started_at=NOW - timedelta(minutes=3),
        url="https://trends.google.com/trends/explore?q=manchester+united+transfer+deadline",
    )

    assert match_story_to_trends(story, [signal], min_searches=10_000) == []


def test_stale_or_undated_trend_signals_are_not_eligible():
    recent = TrendSignal("IN", "INDIA", "recent", "20K+", 20_000, NOW, "https://example.test/recent")
    stale = TrendSignal("IN", "INDIA", "stale", "20K+", 20_000, NOW - timedelta(hours=25), "https://example.test/stale")
    undated = TrendSignal("IN", "INDIA", "undated", "20K+", 20_000, None, "https://example.test/undated")
    fresh, excluded_count = fresh_trend_signals([recent, stale, undated], now=NOW)
    assert fresh == [recent]
    assert excluded_count == 2


def test_trend_search_covers_low_volume_topics_before_trend_gating_and_keeps_publisher_metadata():
    fixture = Path(__file__).parent / "fixtures" / "google_news_search.xml"
    content = fixture.read_bytes()

    class Response:
        def __init__(self):
            self.content = content

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params, timeout):
            self.calls.append((url, params, timeout))
            return Response()

    session = Session()
    signals = [
        TrendSignal("IN", "INDIA", "FIXTURE ONLY: Indian election result", "100K+", 100_000, NOW - timedelta(minutes=4), "https://trends.example.test/in"),
        TrendSignal("IN", "INDIA", "FIXTURE ONLY: low volume query", "2K+", 2_000, NOW - timedelta(minutes=3), "https://trends.example.test/low"),
    ]

    articles, status = collect_trend_news_articles(
        signals,
        session=session,
        retrieved_at=NOW,
        max_queries_per_geo=3,
        min_delay_seconds=0,
    )

    assert len(session.calls) == 2
    assert session.calls[0][0] == "https://news.google.com/rss/search"
    assert session.calls[0][1] == {"q": "FIXTURE ONLY: Indian election result", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    assert session.calls[1][1]["q"] == "FIXTURE ONLY: low volume query"
    assert len(articles) == 2
    assert articles[0].publisher == "Fixture News Publisher"
    assert articles[0].source_domain == "fixture-publisher.example.test"
    assert articles[0].source_type == "google_news_search"
    assert articles[0].published_at == NOW - timedelta(minutes=2)
    assert [item["query"] for item in status] == ["FIXTURE ONLY: Indian election result", "FIXTURE ONLY: low volume query"]
    assert all(item["status"] == "OK" for item in status)


