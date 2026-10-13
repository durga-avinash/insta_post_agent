from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone

from .models import Article, Evidence, Story
from .normalize import clean_html_text, normalize_title, stable_id
from .scoring import cluster_key, trend_score

SENSITIVE_TERMS = re.compile(
    r"\b(alleged|allegedly|accused|attack|bomb|casualties|crash|death|dead|killed|rape|sexual assault|war|wounded|injured|terror|hostage)\b",
    re.IGNORECASE,
)
CONFLICT_CUES = re.compile(r"\b(denied|disputed|contradicted|unclear|unconfirmed|alleged|allegedly|may have)\b", re.IGNORECASE)
WIRE_CUES = {
    "associated press": "wire:ap",
    "ap photos": "wire:ap",
    "reuters": "wire:reuters",
    "afp": "wire:afp",
}
CLAIM_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or",
    "says", "the", "to", "with", "after", "amid", "over", "under", "will", "has", "have", "new", "fixture", "only",
}


def _core_terms(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]{3,}", value.lower()) if word not in CLAIM_STOP_WORDS}


def source_independence_groups(articles: list[Article]) -> dict[str, str]:
    groups: dict[str, str] = {}
    for article in articles:
        content = f"{article.title} {article.description}".lower()
        wire_group = next((group for cue, group in WIRE_CUES.items() if cue in content), None)
        if wire_group:
            groups[article.source_id] = wire_group
        else:
            groups[article.source_id] = article.source_domain or article.source_id
    # Identical feed descriptions often indicate cross-posting; do not count them as independent.
    descriptions: dict[str, list[str]] = {}
    for article in articles:
        normalized = re.sub(r"\W+", " ", article.description.lower()).strip()
        if normalized and len(normalized) > 40:
            descriptions.setdefault(hashlib.sha256(normalized.encode()).hexdigest(), []).append(article.source_id)
    for ids in descriptions.values():
        if len(ids) > 1:
            shared = "copy:" + stable_id(*sorted(ids))
            for source_id in ids:
                groups[source_id] = shared
    return groups


def compare_reports(articles: list[Article]) -> tuple[list[Evidence], list[str], int]:
    groups = source_independence_groups(articles)
    evidence: list[Evidence] = []
    description_mismatches = []
    for article in articles:
        title_terms = _core_terms(article.title)
        description_terms = _core_terms(article.description)
        overlap = title_terms & description_terms
        if article.description and title_terms and not overlap:
            description_mismatches.append(article.source_id)
        evidence.append(
            Evidence(
                url=article.url,
                publisher=article.publisher,
                independence=groups.get(article.source_id, article.source_domain),
                claims_checked=[
                    "Headline and RSS description retained for comparison",
                    f"Title/description key-term overlap: {len(overlap)} of {len(title_terms)} terms",
                ],
                conflicts_or_uncertainty=[],
            )
        )
    descriptions = " ".join(a.description for a in articles)
    if CONFLICT_CUES.search(descriptions):
        for item in evidence:
            item.conflicts_or_uncertainty.append("RSS text contains uncertainty or dispute language")
    if not any(a.description for a in articles):
        for item in evidence:
            item.conflicts_or_uncertainty.append("No source description was available to compare")
    independent_groups = set(groups.values())
    group_count = len(independent_groups)
    reasons: list[str] = []
    if len({a.source_domain for a in articles}) < 2:
        reasons.append("Only one publisher domain was retrieved for this event")
    if group_count < 2:
        reasons.append("Reports are not independently corroborated (same or suspected syndicated source)")
    if not any(a.description for a in articles):
        reasons.append("Source descriptions are missing; key claims cannot be compared")
    if any(a.published_at is None for a in articles):
        reasons.append("One or more publication timestamps are missing or malformed")
    if CONFLICT_CUES.search(descriptions):
        reasons.append("Reports contain uncertainty/dispute cues and require human review")
    if description_mismatches:
        reasons.append("At least one RSS description does not match its headline's key terms")
    if SENSITIVE_TERMS.search(" ".join(a.title for a in articles)):
        reasons.append("Sensitive or developing subject requires human review")
    numeric_sets = [set(re.findall(r"\b\d[\d,.%]*\b", f"{a.title} {a.description}")) for a in articles]
    numeric_sets = [values for values in numeric_sets if values]
    if len(numeric_sets) >= 2:
        shared_numbers = set.intersection(*numeric_sets)
        all_numbers = set.union(*numeric_sets)
        if not shared_numbers or shared_numbers != all_numbers:
            reasons.append("Numeric details differ across reports or are not corroborated; verify against original pages")
    dates = [a.published_at for a in articles if a.published_at]
    if len(dates) > 1 and (max(dates) - min(dates)).total_seconds() > 24 * 3600:
        reasons.append("Publication timestamps span more than 24 hours; review the event chronology")
    score = min(100, 35 + min(group_count, 3) * 20 + (15 if any(a.description for a in articles) else 0))
    return evidence, reasons, score


def deterministic_summary(articles: list[Article], max_chars: int = 190) -> str:
    # Use only source-supplied text; no extra context or causal claims are introduced.
    candidate = next((clean_html_text(a.description) for a in articles if a.description.strip()), "")
    if not candidate:
        return "Source descriptions were unavailable. Open the original report before using this story."
    first = re.split(r"(?<=[.!?])\s+", candidate, maxsplit=1)[0].strip()
    sentence = first or candidate
    if len(sentence) > max_chars:
        clipped = sentence[: max_chars - 1].rsplit(" ", 1)[0].rstrip(".,;:")
        sentence = clipped + "…"
    return sentence


def build_story(articles: list[Article], batch_id: str, now: datetime | None = None) -> Story:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ordered = sorted(articles, key=lambda a: (a.published_at is not None, a.published_at or clock), reverse=True)
    lead = ordered[0]
    evidence, reasons, credibility = compare_reports(ordered)
    reject_terms = ("sponsored", "advertisement", "quiz", "horoscope", "newsletter", "podcast")
    if len(lead.title) < 18 or any(term in lead.title.lower() for term in reject_terms):
        status = "REJECTED"
        reasons.append("Headline appears to be non-news or promotional content")
    elif len({item.independence for item in evidence}) < 2 or not any(a.description for a in ordered):
        status = "NEEDS_REVIEW"
    elif reasons:
        status = "NEEDS_REVIEW"
    else:
        status = "APPROVED"
        reasons.append("At least two distinct source groups reported the event; automated checks found no review flags")
    score, parts = trend_score(ordered, now=clock)
    source_names = list(dict.fromkeys(a.publisher for a in ordered))
    # A publication date is not automatically the date an event happened.
    event_date = ""
    return Story(
        news_id=stable_id(batch_id, cluster_key(ordered)),
        batch_id=batch_id,
        region=lead.region,
        headline=lead.title,
        summary=deterministic_summary(ordered),
        category=lead.category,
        event_date=event_date,
        publisher=", ".join(source_names),
        original_url=lead.url,
        corroborating_urls=[a.url for a in ordered[1:]],
        articles=ordered,
        evidence=evidence,
        trend_score=score,
        score_components=parts,
        verification_status=status,  # type: ignore[arg-type]
        verification_reasons=reasons,
        credibility_score=credibility,
        retrieved_at=max(a.retrieved_at for a in ordered),
    )
