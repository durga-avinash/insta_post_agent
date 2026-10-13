from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import requests

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        base_url = os.getenv("LLM_API_BASE_URL", "").strip().rstrip("/")
        model = os.getenv("LLM_MODEL", "").strip()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if not (base_url and model and api_key):
            return None
        return cls(base_url=base_url, model=model, api_key=api_key)


def validate_editorial_payload(payload: object, source_text: str) -> tuple[str, str] | None:
    if not isinstance(payload, dict) or set(payload) != {"headline", "summary"}:
        return None
    headline, summary = payload.get("headline"), payload.get("summary")
    if not isinstance(headline, str) or not isinstance(summary, str):
        return None
    headline, summary = " ".join(headline.split()), " ".join(summary.split())
    if not headline or len(headline) > 160 or not summary or len(summary) > 360:
        return None
    source_numbers = set(re.findall(r"\b\d[\d,.%]*\b", source_text))
    output_numbers = set(re.findall(r"\b\d[\d,.%]*\b", headline + " " + summary))
    if not output_numbers.issubset(source_numbers):
        return None
    source_tokens = set(re.findall(r"[a-z]{4,}", source_text.lower()))
    output_tokens = set(re.findall(r"[a-z]{4,}", (headline + " " + summary).lower()))
    if output_tokens and len(source_tokens & output_tokens) / len(output_tokens) < 0.40:
        return None
    return headline, summary


def refine_headline_summary(
    headline: str,
    source_descriptions: list[str],
    *,
    config: LLMConfig | None = None,
    session: requests.Session | None = None,
    retries: int = 2,
) -> tuple[str, str] | None:
    """Optional OpenAI-compatible editor; returns None on any issue for safe fallback."""
    settings = config or LLMConfig.from_env()
    if settings is None:
        return None
    evidence = "\n\n".join(text.strip() for text in source_descriptions if text.strip())[:12000]
    source_text = f"{headline}\n{evidence}"
    if not evidence:
        return None
    client = session or requests.Session()
    body = {
        "model": settings.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return JSON with exactly headline and summary. Use only facts present in the supplied text. Do not add dates, numbers, names, causes, or implications. Keep the summary under 360 characters."},
            {"role": "user", "content": json.dumps({"headline": headline, "source_descriptions": source_descriptions}, ensure_ascii=False)},
        ],
    }
    for attempt in range(max(1, retries)):
        try:
            response = client.post(
                f"{settings.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=(8, 25),
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            valid = validate_editorial_payload(parsed, source_text)
            if valid:
                return valid
            body["messages"].append({"role": "assistant", "content": str(content)[:2000]})
            body["messages"].append({"role": "user", "content": "The last result failed validation. Return corrected JSON only."})
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
            LOGGER.warning("Optional editorial model unavailable; deterministic source text will be used")
            return None
    LOGGER.warning("Optional editorial model output failed validation; deterministic source text will be used")
    return None
