from __future__ import annotations

from datetime import timedelta

from insta_post_agent.editor import LLMConfig, refine_headline_summary, validate_editorial_payload
from insta_post_agent.pipeline import _eligible_review_draft
from insta_post_agent.models import Article
from insta_post_agent.scoring import cluster_key
from insta_post_agent.verification import build_story, compare_reports, is_trusted_source_domain, source_independence_groups

from test_news_core import NOW, article


def test_independent_publishers_approve_and_shared_wire_needs_review():
    one = article("Fixture research institute publishes a clean energy report", "https://bbc.com/fixture/a", "bbc.com", description="A fixture research group released an energy report.", source_id="google-news-query")
    two = article("Fixture clean energy report published by a research institute", "https://reuters.com/fixture/b", "reuters.com", description="The synthetic research institute published its clean energy report today.", source_id="google-news-query")
    story = build_story([one, two], "fixture-batch", now=NOW)
    assert story.verification_status == "APPROVED"
    assert len({item.independence for item in story.evidence}) == 2
    assert story.event_date == ""  # publication time is not asserted as event date

    wire_one = article("Fixture research institute publishes a clean energy report", "https://wire-one.example.test/a", "wire-one.example.test", description="Fixture via Reuters: a research institute published a clean energy report.")
    wire_two = article("Fixture clean energy report published by research institute", "https://wire-two.example.test/b", "wire-two.example.test", description="Fixture via Reuters: a research institute published a clean energy report.")
    assert len(set(source_independence_groups([wire_one, wire_two]).values())) == 1
    assert build_story([wire_one, wire_two], "fixture-batch", now=NOW).verification_status == "NEEDS_REVIEW"


def test_single_curated_source_is_enough_but_uncurated_cluster_stays_in_review():
    one = article("Fixture research institute publishes a clean energy report", "https://bbc.com/fixture/a", "bbc.com")
    two = article("Fixture clean energy report published by a research institute", "https://unknown.example.test/b", "unknown.example.test")
    story = build_story([one, two], "fixture-batch", now=NOW)

    assert is_trusted_source_domain("feeds.bbc.co.uk") is True
    assert is_trusted_source_domain("www.reuters.com") is True
    assert is_trusted_source_domain("notreuters.com") is False
    assert story.verification_status == "NEEDS_REVIEW"
    assert any("uncurated publisher" in reason for reason in story.verification_reasons)


def test_uncurated_newer_report_cannot_lead_and_flags_the_story_for_review():
    trusted_recent = article(
        "Fixture district leaders approve a coastal flood plan",
        "https://bbc.com/fixture/flood-plan",
        "bbc.com",
        description="A fixture report says district leaders approve a coastal flood plan.",
        published_at=NOW - timedelta(minutes=9),
    )
    trusted_second = article(
        "Fixture coastal flood plan approved by district leaders",
        "https://theguardian.com/fixture/flood-plan",
        "theguardian.com",
        description="A fixture account says leaders approve the new coastal flood plan.",
        published_at=NOW - timedelta(minutes=12),
    )
    uncurated_newer = article(
        "Fixture newest details: district leaders approve a coastal flood plan",
        "https://unknown.example.test/flood-plan",
        "unknown.example.test",
        description="A fixture summary says district leaders approve a coastal flood plan.",
        published_at=NOW - timedelta(minutes=1),
    )

    story = build_story([uncurated_newer, trusted_second, trusted_recent], "fixture-batch", now=NOW)

    assert story.verification_status == "NEEDS_REVIEW"
    assert story.headline == trusted_recent.title
    assert story.original_url == trusted_recent.url


def test_sensitive_and_insufficient_evidence_gating():
    single = article("Fixture report on a policy planning meeting", "https://one.example.test/a", "one.example.test")
    assert build_story([single], "fixture-batch", now=NOW).verification_status == "NEEDS_REVIEW"
    sensitive_a = article("Fixture earthquake response teams report casualties", "https://one.example.test/a", "one.example.test", description="Fixture report from a synthetic response office.")
    sensitive_b = article("Fixture response teams report earthquake casualties", "https://two.example.test/b", "two.example.test", description="An invented classroom description for the test.")
    story = build_story([sensitive_a, sensitive_b], "fixture-batch", now=NOW)
    assert story.verification_status == "NEEDS_REVIEW"
    assert any("Sensitive" in reason for reason in story.verification_reasons)


def test_review_draft_fallback_rejects_sensitive_conflicted_or_evidence_poor_stories():
    safe = article(
        "Fixture local library publishes a community reading report",
        "https://www.bbc.com/news/fixture-library",
        "bbc.com",
        description="A local library publishes a community reading report for nearby schools.",
    )
    assert _eligible_review_draft(build_story([safe], "fixture-batch", now=NOW)) is False

    sensitive = article(
        "Fixture local report confirms casualties after a crash",
        "https://single.example.test/crash",
        "single.example.test",
        description="A local report confirms casualties after a crash.",
    )
    assert _eligible_review_draft(build_story([sensitive], "fixture-batch", now=NOW)) is False

    uncertain = article(
        "Fixture council's clean energy plan disputed by reviewers",
        "https://single.example.test/energy",
        "single.example.test",
        description="The clean energy plan is disputed by reviewers, according to the report.",
    )
    assert _eligible_review_draft(build_story([uncertain], "fixture-batch", now=NOW)) is False

    missing = article(
        "Fixture local library publishes a community reading report",
        "https://single.example.test/missing",
        "single.example.test",
        description="",
    )
    assert _eligible_review_draft(build_story([missing], "fixture-batch", now=NOW)) is False


def test_mismatched_source_description_requires_review():
    one = article("Fixture clean energy research teams publish report", "https://one.example.test/a", "one.example.test", description="Fixture researchers publish a clean energy report.")
    two = article("Fixture clean energy report from research teams", "https://two.example.test/b", "two.example.test", description="Unrelated classroom note about a fictional cooking exercise.")
    story = build_story([one, two], "fixture-batch", now=NOW)
    assert story.verification_status == "NEEDS_REVIEW"
    assert any("does not match" in reason for reason in story.verification_reasons)


def test_llm_schema_validation_and_deterministic_no_key_fallback():
    source = "Fixture energy researchers publish report about clean power projects."
    assert validate_editorial_payload({"headline": "Clean power fixture report", "summary": "Energy researchers publish a report about clean power."}, source)
    assert validate_editorial_payload({"headline": "New $9 billion project", "summary": "A report."}, source) is None
    assert validate_editorial_payload({"headline": "Headline", "summary": "Short", "source": "invented"}, source) is None
    assert refine_headline_summary("Fixture headline", [source], config=None) is None


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeSession:
    def __init__(self, contents):
        self.contents = iter(contents)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        import json
        return FakeResponse(json.dumps(next(self.contents)))


def test_llm_retries_malformed_schema_then_returns_validated_result():
    config = LLMConfig("https://model.example.test/v1", "fixture-model", "fixture-secret")
    session = FakeSession([
        {"unexpected": "bad schema"},
        {"headline": "Fixture clean energy research report", "summary": "Fixture researchers publish a clean energy report."},
    ])
    result = refine_headline_summary(
        "Fixture clean energy research report",
        ["Fixture researchers publish a clean energy report."],
        config=config,
        session=session,
    )
    assert result is not None
    assert session.calls == 2
