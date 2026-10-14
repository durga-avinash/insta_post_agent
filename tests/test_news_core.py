from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from insta_post_agent.collector import parse_feed_content
from insta_post_agent.collector import feed_error_detail, make_session
from insta_post_agent.classify import classify_category, classify_region, is_mba_relevant
from insta_post_agent.models import Article, FeedSource
from insta_post_agent.normalize import canonicalize_url, normalize_timestamp
from insta_post_agent.scoring import cluster_articles, deduplicate_articles, trend_score
from insta_post_agent.sources import load_sources

NOW = datetime(2026, 10, 13, 3, 30, tzinfo=timezone.utc)


def article(title: str, url: str, domain: str, *, description: str = "A fixture report for deterministic tests.", region: str = "INDIA", published_at=NOW - timedelta(minutes=20), source_id: str | None = None) -> Article:
    return Article(
        id=f"id-{domain}-{title[:8]}", title=title, publisher=domain.title(), url=url,
        canonical_url=canonicalize_url(url), published_at=published_at, retrieved_at=NOW,
        region=region, category=classify_category(title, description), description=description,
        source_id=source_id or domain, source_type="publisher", source_domain=domain,
    )


def test_rss_parsing_timestamp_normalization_and_malformed_entries():
    fixture = Path(__file__).parent / "fixtures" / "sample_rss.xml"
    source = FeedSource("fixture", "Fixture Publisher", "https://fixture.example.test/rss", "INDIA", "Test")
    parsed = parse_feed_content(source, fixture.read_bytes(), retrieved_at=NOW)
    assert len(parsed) == 2  # entry without a valid article URL is ignored
    assert parsed[0].title.startswith("FIXTURE ONLY:")
    assert parsed[0].published_at == NOW - timedelta(minutes=30)
    assert parsed[1].published_at is None
    assert parsed[0].canonical_url == "https://fixture.example.test/news/river-sensors"


def test_google_news_publisher_suffix_cannot_create_a_false_business_topic():
    source = FeedSource(
        "google-news-fixture", "Google News fixture", "https://news.google.com/rss/search",
        "INDIA", "Fixture", source_type="google_news_search",
    )
    content = b"""<rss version='2.0'><channel><item>
      <title>FIXTURE ONLY: LASK vs Liverpool Champions League preview - NDTV Profit</title>
      <link>https://news.google.com/rss/articles/FIXTURE_ONLY</link>
      <source url='https://www.ndtv.com/'>NDTV Profit</source>
      <description>FIXTURE ONLY: A synthetic football match preview.</description>
      <pubDate>Tue, 13 Oct 2026 03:00:00 GMT</pubDate>
    </item></channel></rss>"""

    parsed = parse_feed_content(source, content, retrieved_at=NOW)

    assert len(parsed) == 1
    assert parsed[0].title == "FIXTURE ONLY: LASK vs Liverpool Champions League preview"
    assert not is_mba_relevant(parsed[0].title, parsed[0].description)


def test_timestamp_and_url_canonicalization():
    assert normalize_timestamp("2026-10-13T03:00:00Z") == datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
    assert normalize_timestamp("not a date") is None
    assert canonicalize_url("HTTP://WWW.Example.com:80/a/?utm_campaign=x&b=2#part") == "https://example.com/a?b=2"


def test_url_deduplication_and_story_clustering():
    a = article("Fixture: river sensors added across sample districts", "https://news.one.test/a?utm_source=x", "news.one.test")
    duplicate = article("Same event with alternate link", "https://www.news.one.test/a", "news.one.test")
    corroboration = article("Fixture sample districts add river sensors", "https://news.two.test/story", "news.two.test")
    unrelated = article("Fixture orchestra schedules summer concert", "https://news.three.test/story", "news.three.test")
    assert len(deduplicate_articles([a, duplicate])) == 1
    clusters = cluster_articles([a, corroboration, unrelated])
    assert sorted(len(cluster) for cluster in clusters) == [1, 2]


def test_region_category_and_transparent_trend_components():
    assert classify_region("WORLD", "Indian court issues fixture ruling", "Fixture text") == "INDIA"
    assert classify_region("WORLD", "Fixture climate talks begin", "A synthetic climate meeting") == "WORLD"
    assert classify_category("Fixture central bank decision", "A synthetic policy note") == "Economy & Policy"
    first = article("Fixture election policy report", "https://one.example.test/a", "one.example.test")
    second = article("Fixture election policy report update", "https://two.example.test/b", "two.example.test")
    score, components = trend_score([first, second], now=NOW)
    assert score == sum(components.values())
    assert components["independent_coverage_proxy"] == 20
    assert components["search_interest"] == 0


def test_mba_relevance_uses_headline_topic_and_rejects_general_interest_false_positives():
    assert is_mba_relevant("RBI holds rates as inflation cools", "Central bank policy update")
    assert is_mba_relevant("New vaccine trial advances to phase three", "Healthcare research update")
    assert not is_mba_relevant("Fresh summons in a YouTuber defamation case", "A court issued another summons")
    assert not is_mba_relevant("Rapper injured in prison fight and taken to hospital", "A hospital admitted a public figure")
    assert not is_mba_relevant("Cricket live score: India vs West Indies", "Trade talks continue before the match")
    assert not is_mba_relevant("Football transfer talks continue", "The team is discussing a trade")


def test_feed_retry_policy_and_source_registry_load():
    session = make_session()
    retry = session.get_adapter("https://").max_retries
    assert retry.total == 3
    assert {429, 500, 502, 503, 504}.issubset(set(retry.status_forcelist))
    session.close()
    sources = load_sources(Path(__file__).parents[1] / "config" / "sources.json")
    assert {source.region for source in sources} == {"INDIA", "WORLD"}
    source_ids = {source.id for source in sources}
    assert {"business-standard-india", "business-standard-markets", "hindustan-times-india", "hindustan-times-business", "bbc-india", "pib-india"}.issubset(source_ids)
    assert len(sources) >= 8


def test_feed_error_detail_preserves_http_status_without_logging_response_body():
    import requests

    error = requests.HTTPError("blocked response body")
    error.response = type("Response", (), {"status_code": 403, "text": "private response detail"})()
    assert feed_error_detail(error) == "HTTP 403"
    assert feed_error_detail(requests.Timeout()) == "Timeout"

