from __future__ import annotations

import re

CATEGORY_RULES = [
    ("Economy", r"\b(economy|inflation|budget|markets?|stocks?|rupee|trade|bank|tax|jobs?)\b"),
    ("Climate", r"\b(climate|heatwave|flood|cyclone|wildfire|emissions|weather)\b"),
    ("Health", r"\b(health|hospital|vaccine|disease|outbreak|medical)\b"),
    ("Science & Technology", r"\b(science|space|satellite|solar|research|technology|AI|artificial intelligence)\b"),
    ("Courts & Law", r"\b(court|judge|supreme court|verdict|ruling|legal|lawsuit)\b"),
    ("Politics & Policy", r"\b(election|parliament|minister|government|policy|president|prime minister)\b"),
    ("Conflict & Security", r"\b(attack|war|missile|military|strike|killed|casualties|ceasefire)\b"),
    ("Sports", r"\b(sport|cricket|football|tennis|olympic|tournament|match|athlete)\b"),
]


def classify_category(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    for label, pattern in CATEGORY_RULES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return "General"


def classify_region(source_region: str, title: str, description: str) -> str:
    """Keep source section as the primary signal; correct clear India-specific cases."""
    text = f"{title} {description}".lower()
    india_signal = re.search(
        r"\b(india|indian|new delhi|mumbai|kolkata|chennai|bengaluru|modi|rupee|lok sabha|rajya sabha)\b",
        text,
    )
    return "INDIA" if india_signal else source_region.upper()
