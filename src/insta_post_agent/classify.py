from __future__ import annotations

import re


CATEGORY_RULES = [
    ("Business & Markets", r"\b(business|company|companies|corporate|earnings|revenue|profit|loss|merger|acquisition|ipo|stocks?|shares|investors?|investment|startup|valuation|layoffs?|executive|ceo|bankruptcy|deal|capital|funding|market)\b"),
    ("Economy & Policy", r"\b(economy|economic|gdp|inflation|budget|fiscal|monetary|central bank|reserve bank|rbi|interest rates?|repo rate|tax|tariff|trade|exports?|imports?|rupee|employment|jobs?|wages?|government|policy|regulation|parliament|minister|election|president|prime minister|supreme court|court ruling)\b"),
    ("Geopolitics & Security", r"\b(geopolitics?|diplomacy|diplomatic|sanctions?|ceasefire|conflict|war|military|defence|defense|security|border|treaty|alliance|nato|united nations|iran|israel|ukraine|russia|china|pakistan|trade war)\b"),
    ("Healthcare", r"\b(healthcare|health|hospital|clinic|clinics|laboratory|laboratories|lab|protein|pharma|pharmaceutical|drug trial|clinical trial|vaccine|disease|outbreak|public health|medical|medicine|who)\b"),
    ("Technology & Industry", r"\b(technology|tech|artificial intelligence|\bai\b|semiconductor|chipmaker|data centre|data center|cyber|telecom|software|robotics|research|patent|manufacturing|industry|industrial|automotive|electric vehicle|evs?)\b"),
    ("Climate & Energy", r"\b(climate|emissions|carbon|renewable|solar|wind power|clean energy|energy transition|oil|gas|electricity|power grid|heatwave|flood|cyclone|wildfire|ice retreat|polar ice|mangrove|ecosystem|biodiversity|water monitoring|clean water)\b"),
    ("Infrastructure & Supply Chains", r"\b(supply chain|logistics|shipping|port|rail|railway|airport|infrastructure|freight|transport|transit|commuter|sensor|semiconductor supply|construction|commodities|commodity)\b"),
    ("Sports", r"\b(sport|cricket|football|tennis|olympic|tournament|match|athlete|league|championship)\b"),
]

MBA_PRIORITY_CATEGORIES = frozenset({
    "Business & Markets", "Economy & Policy", "Geopolitics & Security", "Healthcare",
    "Technology & Industry", "Climate & Energy", "Infrastructure & Supply Chains",
})


def classify_category(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    for label, pattern in CATEGORY_RULES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return "General"


def mba_relevance_score(title: str, description: str) -> float:
    """A transparent topic-fit score, not a measure of story importance or truth."""
    category = classify_category(title, description)
    if category not in MBA_PRIORITY_CATEGORIES:
        return 0.0
    title_category = classify_category(title, "")
    return 0.55 if title_category in MBA_PRIORITY_CATEGORIES else 0.35


def is_mba_relevant(title: str, description: str) -> bool:
    """Exclude general-interest items unless the source text gives a domain signal."""
    return mba_relevance_score(title, description) > 0.0


def classify_region(source_region: str, title: str, description: str) -> str:
    """Keep source section as the primary signal; correct clear India-specific cases."""
    text = f"{title} {description}".lower()
    india_signal = re.search(
        r"\b(india|indian|new delhi|mumbai|kolkata|chennai|bengaluru|modi|rupee|lok sabha|rajya sabha)\b",
        text,
    )
    return "INDIA" if india_signal else source_region.upper()

