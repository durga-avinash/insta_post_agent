from __future__ import annotations

from .models import Story


def make_caption(stories: list[Story], *, max_chars: int = 2200) -> str:
    has_review_items = any(story.verification_status == "NEEDS_REVIEW" for story in stories)
    if has_review_items:
        intro = "Review draft: NEEDS_REVIEW stories are not independently corroborated. Check each linked report before publication."
        closing = "Read the original sources for full context before manually publishing."
    else:
        intro = "Today's news, with the source links kept close."
        closing = "A concise daily brief. Please read the linked reporting for full context."
    hashtags = "#IndiaNews #WorldNews #DailyBrief"
    fixed: list[str] = []
    summaries: list[str] = []
    for index, story in enumerate(stories, start=1):
        fixed.append(f"{index}. [{story.verification_status}] {story.headline}\nSource: {story.publisher}\n{story.original_url}")
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

