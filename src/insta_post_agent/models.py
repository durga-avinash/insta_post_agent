from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

Region = Literal["INDIA", "WORLD"]
VerificationStatus = Literal["APPROVED", "NEEDS_REVIEW", "REJECTED"]


@dataclass(frozen=True)
class FeedSource:
    id: str
    name: str
    url: str
    region: Region
    section: str
    source_type: str = "publisher"


@dataclass(frozen=True)
class TrendFeed:
    geo: str
    region: Region
    name: str
    url: str


@dataclass
class TrendSignal:
    geo: str
    region: Region
    query: str
    approx_traffic: str
    approximate_searches: int | None
    started_at: datetime | None
    url: str
    news_items: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["started_at"] = self.started_at.isoformat() if self.started_at else None
        return value


@dataclass
class Article:
    id: str
    title: str
    publisher: str
    url: str
    canonical_url: str
    published_at: datetime | None
    retrieved_at: datetime
    region: Region
    category: str
    description: str
    source_id: str
    source_type: str
    source_domain: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["published_at"] = self.published_at.isoformat() if self.published_at else None
        value["retrieved_at"] = self.retrieved_at.isoformat()
        return value


@dataclass
class Evidence:
    url: str
    publisher: str
    independence: str
    claims_checked: list[str] = field(default_factory=list)
    conflicts_or_uncertainty: list[str] = field(default_factory=list)


@dataclass
class Story:
    news_id: str
    batch_id: str
    region: Region
    headline: str
    summary: str
    category: str
    event_date: str
    publisher: str
    original_url: str
    corroborating_urls: list[str]
    articles: list[Article]
    evidence: list[Evidence]
    trend_score: float
    score_components: dict[str, float]
    verification_status: VerificationStatus
    verification_reasons: list[str]
    credibility_score: int
    retrieved_at: datetime
    image_path: str = ""
    caption: str = ""
    freshness_status: str = "FRESH"
    trend_status: str = "UNCONFIRMED"
    trend_evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "news_id": self.news_id,
            "batch_id": self.batch_id,
            "region": self.region,
            "headline": self.headline,
            "summary": self.summary,
            "category": self.category,
            "event_date": self.event_date,
            "publisher": self.publisher,
            "original_url": self.original_url,
            "corroborating_urls": self.corroborating_urls,
            "articles": [article.to_dict() for article in self.articles],
            "evidence": [asdict(item) for item in self.evidence],
            "trend_score": self.trend_score,
            "score_components": self.score_components,
            "verification_status": self.verification_status,
            "verification_reasons": self.verification_reasons,
            "credibility_score": self.credibility_score,
            "retrieved_at": self.retrieved_at.isoformat(),
            "image_path": self.image_path,
            "caption": self.caption,
            "freshness_status": self.freshness_status,
            "trend_status": self.trend_status,
            "trend_evidence": self.trend_evidence,
        }
