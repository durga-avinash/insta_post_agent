from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from .models import Article, Evidence, Story
from .normalize import clean_html_text, normalize_domain, normalize_title, stable_id
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
# Only publishers with established editorial standards or primary public-service
# domains can count toward an automatic APPROVED decision. A new source can still
# appear in the evidence log, but it remains a human-review source until curated.
TRUSTED_SOURCE_DOMAINS = frozenset({
    "apnews.com", "reuters.com", "afp.com", "bbc.com", "bbc.co.uk", "bbc.in",
    "theguardian.com", "npr.org", "thehindu.com", "indianexpress.com", "business-standard.com",
    "hindustantimes.com", "timesofindia.indiatimes.com", "ndtv.com", "indiatoday.in",
    "scroll.in", "theprint.in", "livemint.com", "moneycontrol.com",
    "economictimes.indiatimes.com", "ndtvprofit.com", "yahoo.com", "nytimes.com", "washingtonpost.com", "wsj.com",
    "ft.com", "cnn.com", "cbsnews.com", "pbs.org", "nbcnews.com", "abcnews.go.com",
    "dw.com", "aljazeera.com", "france24.com", "abc.net.au", "cbc.ca", "nhk.or.jp",
    "espn.com", "espn.in", "espncricinfo.com", "cricbuzz.com",
    "pib.gov.in", "rbi.org.in", "who.int", "un.org", "europa.eu",
})
CLAIM_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or",
    "says", "the", "to", "with", "after", "amid", "over", "under", "will", "has", "have", "new", "fixture", "only",
}


def _core_terms(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]{3,}", value.lower()) if word not in CLAIM_STOP_WORDS}


def is_trusted_source_domain(value: str) -> bool:
    host = value.lower().strip().rstrip(".")
    if "://" in host:
        host = (urlparse(host).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in TRUSTED_SOURCE_DOMAINS) or host.endswith((".gov", ".gov.in", ".gov.uk", ".nic.in"))


def _article_evidence_key(article: Article) -> str:
    # One Google News query feed has one source_id even when it contains many
    # publishers. Count each original publisher article independently instead.
    return article.canonical_url or article.id or f"{article.source_id}:{article.source_domain}:{article.url}"


def source_independence_groups(articles: list[Article]) -> dict[str, str]:
    groups: dict[str, str] = {}
    for article in articles:
        content = f"{article.title} {article.description}".lower()
        wire_group = next((group for cue, group in WIRE_CUES.items() if cue in content), None)
        groups[_article_evidence_key(article)] = wire_group or normalize_domain(article.source_domain) or article.source_id
    # Identical feed descriptions often indicate cross-posting; do not count them as independent.
    descriptions: dict[str, list[str]] = {}
    for article in articles:
        normalized = re.sub(r"\W+", " ", article.description.lower()).strip()
        if normalized and len(normalized) > 40:
            descriptions.setdefault(hashlib.sha256(normalized.encode()).hexdigest(), []).append(_article_evidence_key(article))
    for ids in descriptions.values():
        if len(ids) > 1:
            shared = "copy:" + stable_id(*sorted(ids))
            for evidence_key in ids:
                groups[evidence_key] = shared
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
                independence=groups.get(_article_evidence_key(article), article.source_domain),
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
    trusted_group_count = len({
        groups.get(_article_evidence_key(article), article.source_domain)
        for article in articles
        if is_trusted_source_domain(article.source_domain)
    })
    reasons: list[str] = []
    if len({normalize_domain(a.source_domain) for a in articles if a.source_domain}) < 2:
        note = "Only one publisher domain was retrieved; this is recorded as limited coverage, not a truth failure"
        for item in evidence:
            item.claims_checked.append(note)
    elif group_count < 2 or trusted_group_count < 2:
        note = "Publisher copies were grouped as one source; they do not count as independent pickup"
        for item in evidence:
            item.claims_checked.append(note)
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


def _source_sentences(articles: list[Article]) -> list[tuple[str, str, str]]:
    """Return unique, source-authored sentences with their source and independence IDs."""
    groups = source_independence_groups(articles)
    candidates: list[tuple[str, str, str]] = []
    seen: list[set[str]] = []
    for article in articles:
        text = clean_html_text(article.description)
        for raw in re.split(r"(?<=[.!?])\s+|\s+[|•]\s+", text):
            sentence = raw.strip(" \t\r\n-–—•")
            if len(sentence) < 26:
                continue
            tokens = _core_terms(sentence)
            if not tokens:
                continue
            normalized_sentence = normalize_title(sentence)
            normalized_headline = normalize_title(article.title)
            if normalized_headline and normalized_headline in normalized_sentence:
                # Search feeds sometimes put the full title in description as
                # well. Omit that literal echo and use another source detail if
                # one exists; avoid a broad token-overlap filter that would also
                # discard a genuine paraphrase carrying useful context.
                continue
            if any(
                len(tokens & prior) / max(1, len(tokens | prior)) >= 0.72
                or len(tokens & prior) / max(1, min(len(tokens), len(prior))) >= 0.86
                for prior in seen
            ):
                continue
            seen.append(tokens)
            candidates.append((sentence, article.source_domain, groups.get(_article_evidence_key(article), article.source_domain)))
    return candidates


def _fit_sentences(sentences: list[str], max_chars: int) -> list[str]:
    result: list[str] = []
    used = 0
    for sentence in sentences:
        separator = 1 if result else 0
        remaining = max_chars - used - separator
        if remaining < 28:
            break
        if len(sentence) > remaining:
            clipped = sentence[: remaining - 1].rsplit(" ", 1)[0].rstrip(".,;:")
            if len(clipped) >= 24:
                result.append(clipped + "…")
            break
        result.append(sentence)
        used += separator + len(sentence)
    return result


def deterministic_summary(articles: list[Article], max_chars: int = 330) -> str:
    """Merge distinct source descriptions extractively; never add facts or causal claims."""
    candidates = _source_sentences(articles)
    if not candidates:
        return "Source descriptions were unavailable. Open the original report before using this story."
    # Keep the trusted lead's sentence first, then add a distinct line from
    # another independent publisher when present. Both remain source-authored
    # excerpts, so merging does not silently turn into model-authored fact.
    chosen = [candidates[0][0]]
    lead_group = candidates[0][2]
    second = next((sentence for sentence, _, group in candidates[1:] if group != lead_group), None)
    if second:
        chosen.append(second)
    return " ".join(_fit_sentences(chosen, max_chars))


def deterministic_key_takeaways(articles: list[Article], limit: int = 3) -> list[str]:
    """Select source-authored details that add information beyond the summary."""
    summary = deterministic_summary(articles)
    summary_tokens = [
        _core_terms(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+", summary)
        if _core_terms(sentence)
    ]
    chosen: list[str] = []
    chosen_tokens: list[set[str]] = []
    for sentence, _, _ in _source_sentences(articles):
        tokens = _core_terms(sentence)
        repeated = any(
            len(tokens & prior) / max(1, len(tokens | prior)) >= 0.42
            or len(tokens & prior) / max(1, min(len(tokens), len(prior))) >= 0.72
            for prior in [*summary_tokens, *chosen_tokens]
        )
        if repeated:
            continue
        chosen.append(sentence)
        chosen_tokens.append(tokens)
        if len(chosen) >= max(0, limit):
            break
    return chosen


def build_story(articles: list[Article], batch_id: str, now: datetime | None = None) -> Story:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # Anchor the displayed headline and deterministic summary to a curated
    # publisher whenever one is present. Uncurated coverage may help reveal
    # conflicts, but it must not become the carousel's lead attribution.
    ordered = sorted(
        articles,
        key=lambda a: (
            is_trusted_source_domain(a.source_domain),
            a.published_at is not None,
            a.published_at or clock,
        ),
        reverse=True,
    )
    lead = ordered[0]
    evidence, reasons, credibility = compare_reports(ordered)
    reject_terms = ("sponsored", "advertisement", "quiz", "horoscope", "newsletter", "podcast")
    if len(lead.title) < 18 or any(term in lead.title.lower() for term in reject_terms):
        status = "REJECTED"
        reasons.append("Headline appears to be non-news or promotional content")
    elif not any(is_trusted_source_domain(article.source_domain) for article in ordered):
        status = "NEEDS_REVIEW"
        reasons.append("No curated trusted publisher or primary source supports this story")
    elif any(not is_trusted_source_domain(article.source_domain) for article in ordered):
        status = "NEEDS_REVIEW"
        reasons.append("An uncurated publisher is present in the event cluster; inspect its claim against the trusted source")
    elif not any(a.description for a in ordered):
        status = "NEEDS_REVIEW"
    elif reasons:
        status = "NEEDS_REVIEW"
    else:
        status = "APPROVED"
        independent = len({item.independence for item in evidence})
        reasons.append(
            "Curated source and automated consistency checks passed; report is not independently corroborated"
            if independent < 2
            else "At least two distinct source groups reported the event; automated checks found no review flags"
        )
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
