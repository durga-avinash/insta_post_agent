from __future__ import annotations

from .models import Story


def make_caption(stories: list[Story], *, max_chars: int = 2200) -> str:
    has_review_items = any(story.verification_status == "NEEDS_REVIEW" for story in stories)
    has_editorial_backup = any(story.selection_basis == "EDITORIAL_BACKUP" for story in stories)
    has_coverage_signal = any(story.selection_basis == "CROSS_PUBLISHER_COVERAGE" for story in stories)
    confirmed_statuses = {"CONFIRMED_GOOGLE_TRENDS", "CONFIRMED_CROSS_PUBLISHER_COVERAGE"}
    has_unconfirmed_trend = any(story.trend_status not in confirmed_statuses for story in stories)
    if has_coverage_signal:
        intro = "Google Trends is used when available. The fallback flags pickup by at least two independent trusted publishers in 24 hours; that is not a search-volume or social-media count."
        if has_editorial_backup:
            intro += " Editor-backup items below do not meet either trend signal."
        closing = "Review the linked originals and any review flags; approve and publish manually."
    elif has_editorial_backup:
        intro = "EDITORIAL BACKUP: Google Trends did not confirm these stories as trending today. They are current MBA-relevant drafts from trusted reporting; review before use."
        closing = "Open each original report and approve manually; do not describe backup stories as Google-Trends-confirmed."
    elif has_unconfirmed_trend:
        intro = "Review draft: items marked TREND UNCONFIRMED have no same-region Google Trends match; do not describe them as trending."
        closing = "Open each original report, resolve review flags, and publish manually only after editorial approval."
    elif has_review_items:
        intro = "Review draft: NEEDS_REVIEW items have unresolved evidence or editorial flags; they are not publication-approved."
        closing = "Open each original report, resolve review flags, and publish manually only after editorial approval."
    else:
        intro = "Today's news, with the source links kept close."
        closing = "A concise daily brief. Please read the linked reporting for full context."
    hashtags = "#IndiaNews #WorldNews #DailyBrief"
    fixed: list[str] = []
    summaries: list[str] = []
    for index, story in enumerate(stories, start=1):
        if story.selection_basis == "EDITORIAL_BACKUP":
            trend_label = "EDITOR BACKUP · NOT TREND-CONFIRMED"
        elif story.selection_basis == "CROSS_PUBLISHER_COVERAGE":
            count = next((item.get("independent_publisher_count") for item in story.trend_evidence if item.get("signal_type") == "independent_publisher_coverage"), 0)
            trend_label = f"WIDE COVERAGE · {count}+ INDEPENDENT PUBLISHERS"
        elif story.trend_status == "CONFIRMED_GOOGLE_TRENDS":
            trend_label = "GOOGLE TRENDS MATCHED"
        else:
            trend_label = "TREND UNCONFIRMED"
        links = list(dict.fromkeys(article.url for article in story.articles if article.url))
        links.extend(url for url in [story.original_url, *story.corroborating_urls] if url and url not in links)
        source_links = "\n".join(links)
        fixed.append(f"{index}. [{story.verification_status}] {story.headline} [{trend_label}]\nSource(s): {story.publisher}\n{source_links}")
        summaries.append(story.summary)
    fixed_text_len = sum(len(section) for section in fixed) + max(0, len(fixed) - 1) * 2
    reserve = len(intro) + len(closing) + len(hashtags) + 8
    summary_budget = max(0, (max_chars - fixed_text_len - reserve) // max(1, len(summaries)))
    sections = []
    for section, summary in zip(fixed, summaries):
        if summary_budget and len(summary) > summary_budget:
            summary = summary[: max(1, summary_budget - 1)].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
        sections.append(section.replace("\nSource:", "\n" + summary + "\nSource:") if summary else section)
    lines = [intro, "", *sections, "", closing, hashtags]
    result = "\n\n".join(part for part in lines if part)
    if len(result) <= max_chars:
        return result
    # If very long source titles/URLs dominate, retain every attribution link and headline.
    compact = "\n".join([intro, *sections, hashtags])
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars]
