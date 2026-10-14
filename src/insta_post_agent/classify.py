from __future__ import annotations

import re


CATEGORY_RULES = [
    ("Business & Markets", r"\b(business|company|companies|corporate|earnings|revenue|profit|profits|merger|acquisition|ipo|stocks?|shares|investors?|investment|startup|valuation|layoffs?|executive|ceo|bankruptcy|capital|funding|market cap|stock market|quarterly results|venture capital|equity|bond yields?|banking|credit|insurance|sponsorship|broadcast rights)\b"),
    ("Economy & Policy", r"\b(economy|economic|gdp|inflation|budget|fiscal|monetary|central bank|reserve bank|rbi|interest rates?|repo rate|tax|tariff|trade policy|trade agreement|exports?|imports?|rupee|employment|unemployment|wages?|government spending|economic policy|regulation|parliament|finance minister|prime minister|supreme court ruling)\b"),
    ("Geopolitics & Security", r"\b(geopolitics?|diplomacy|diplomatic|sanctions?|ceasefire|armed conflict|conflict|war|military|defence|defense|security|border clash|invasion|treaty|alliance|nato|united nations|trade war|blockade|missile strike|airstrike)\b"),
    ("Healthcare", r"\b(healthcare|public health|hospital|clinic|clinics|laboratory|laboratories|biotech|biotechnology|pharma|pharmaceutical|drug approval|drug trial|clinical trial|vaccine|disease outbreak|outbreak|epidemic|pandemic|medical device|health ministry|health policy)\b"),
    ("Technology & Industry", r"\b(technology|tech sector|artificial intelligence|\bai\b|semiconductor|chipmaker|data centre|data center|cybersecurity|cyber attack|telecom|software company|robotics|patent|manufacturing|industry|industrial|automotive|electric vehicle|evs?)\b"),
    ("Climate & Energy", r"\b(climate|emissions|carbon|renewable|solar|wind power|clean energy|energy transition|oil prices?|natural gas|electricity|power grid|heatwave|flood|cyclone|wildfire|ice retreat|polar ice|mangrove|ecosystem|biodiversity|water monitoring|water quality|clean water)\b"),
    ("Infrastructure & Supply Chains", r"\b(supply chain|logistics|shipping|ports?|rail|railway|airport|infrastructure|freight|transport|transit|metro station|semiconductor supply|construction|commodities|commodity prices?|container costs?)\b"),
    ("Sports", r"\b(sport|cricket|football|tennis|olympic|tournament|match|athlete|league|championship)\b"),
]

MBA_PRIORITY_CATEGORIES = frozenset({
    "Business & Markets", "Economy & Policy", "Geopolitics & Security", "Healthcare",
    "Technology & Industry", "Climate & Energy", "Infrastructure & Supply Chains",
})

_MBA_SIGNAL = re.compile(
    r"\b(earnings|revenue|profits?|merger|acquisition|ipo|stocks?|shares|investors?|investment|startup|valuation|layoffs?|ceo|bankruptcy|funding|market cap|stock market|quarterly results|venture capital|equity|bond yields?|banking|credit|insurance|sponsorship|broadcast rights|"
    r"economy|economic|gdp|inflation|budget|fiscal|monetary|central bank|reserve bank|rbi|interest rates?|repo rate|tax|tariff|trade policy|trade agreement|exports?|imports?|rupee|employment|unemployment|wages?|government spending|economic policy|regulation|finance minister|"
    r"geopolitics?|diplomacy|diplomatic|sanctions?|ceasefire|armed conflict|conflict|war|military|defence|defense|security|border clash|invasion|treaty|nato|united nations|trade war|blockade|missile strike|airstrike|"
    r"healthcare|public health|hospital|clinics?|biotech|biotechnology|pharma|pharmaceutical|drug approval|drug trial|clinical trial|vaccine|disease outbreak|outbreak|epidemic|pandemic|medical device|health ministry|health policy|"
    r"technology|tech sector|artificial intelligence|semiconductor|chipmaker|data centre|data center|cybersecurity|cyber attack|telecom|software company|robotics|patent|manufacturing|industrial|automotive|electric vehicle|"
    r"climate|emissions|carbon|renewable|solar|wind power|clean energy|energy transition|oil prices?|natural gas|electricity|power grid|heatwave|flood|cyclone|wildfire|ice retreat|polar ice|mangrove|ecosystem|biodiversity|water monitoring|water quality|clean water|"
    r"supply chain|logistics|shipping|ports?|rail(ways?)?|airport|infrastructure|freight|transport|transit station|metro station|semiconductor supply|commodities|commodity prices?|container costs?)\b",
    flags=re.IGNORECASE,
)
_SPORTS_CONTEXT = re.compile(r"\b(cricket|football|soccer|tennis|nba|nfl|t20|tournament|innings|wicket|goal|matchday|transfer window|champions league)\b", re.I)
_GENERAL_INTEREST_CONTEXT = re.compile(
    r"\b(youtuber|influencer|actor|actress|singer|rapper|celebrity|film|movie|entertainment|prison fight|defamation case|viral video)\b",
    flags=re.IGNORECASE,
)
_SPECIFIC_BUSINESS_SIGNAL = re.compile(
    r"\b(earnings|revenue|profits?|merger|acquisition|ipo|stock market|market cap|quarterly results|venture capital|funding|valuation|bond yields?|interest rates?|inflation|gdp|tax|tariff|trade policy|trade agreement|exports?|imports?|sanctions?|ceasefire|war|armed conflict|drug approval|clinical trial|vaccine|outbreak|biotech|biotechnology|semiconductor|cybersecurity|renewable|oil prices?|power grid|supply chain|shipping|infrastructure|water quality|clean water|ecosystem|biodiversity|ice retreat|polar ice|mangrove|transit station|metro station|rail(ways?)?)\b",
    flags=re.IGNORECASE,
)


def classify_category(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    for label, pattern in CATEGORY_RULES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return "General"


def mba_relevance_score(title: str, description: str) -> float:
    """A transparent topic-fit score, not a measure of story importance or truth."""
    title = title or ""
    description = description or ""
    title_category = classify_category(title, "")
    title_match = bool(_MBA_SIGNAL.search(title)) and title_category in MBA_PRIORITY_CATEGORIES
    # Do not let a stray word in a long cricket/celebrity description turn a
    # general-interest headline into an MBA story. Description-only inclusion
    # requires a specific topic phrase, and sport headlines need an explicit
    # business/economic angle in the headline itself.
    if _SPORTS_CONTEXT.search(title) and not _SPECIFIC_BUSINESS_SIGNAL.search(title):
        return 0.0
    if _GENERAL_INTEREST_CONTEXT.search(title) and not _SPECIFIC_BUSINESS_SIGNAL.search(title):
        return 0.0
    if title_match:
        return 1.0
    combined = f"{title} {description}"
    category = classify_category(title, description)
    if category not in MBA_PRIORITY_CATEGORIES or not _SPECIFIC_BUSINESS_SIGNAL.search(combined):
        return 0.0
    return 0.45


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

