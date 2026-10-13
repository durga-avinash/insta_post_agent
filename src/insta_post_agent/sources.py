from __future__ import annotations

import json
from pathlib import Path

from .models import FeedSource


def load_sources(path: Path) -> list[FeedSource]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources: list[FeedSource] = []
    for item in raw:
        region = str(item["region"]).upper()
        if region not in {"INDIA", "WORLD"}:
            raise ValueError(f"Invalid region in source {item.get('id')}: {region}")
        sources.append(
            FeedSource(
                id=str(item["id"]),
                name=str(item["name"]),
                url=str(item["url"]),
                region=region,  # type: ignore[arg-type]
                section=str(item.get("section", "News")),
                source_type=str(item.get("source_type", "publisher")),
            )
        )
    return sources
