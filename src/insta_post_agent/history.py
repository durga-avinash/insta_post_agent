from __future__ import annotations

import sqlite3
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .scoring import titles_describe_same_event


class NewsHistory:
    """Runner-local store. GitHub Actions restores/saves this file with its cache."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS selected_events (event_key TEXT NOT NULL, signature TEXT NOT NULL DEFAULT '', selected_at TEXT NOT NULL, batch_id TEXT NOT NULL, PRIMARY KEY (event_key, selected_at))"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS completed_batches (batch_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL, summary_json TEXT NOT NULL)"
        )
        self.connection.commit()

    def seen_recently(self, event_key: str, *, signature: str = "", days: int = 2, now: datetime | None = None) -> bool:
        clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (clock - timedelta(days=days)).isoformat()
        rows = self.connection.execute(
            "SELECT event_key, signature FROM selected_events WHERE selected_at >= ?",
            (cutoff,),
        ).fetchall()
        current_titles = [title.strip() for title in signature.splitlines() if title.strip()]
        for prior_key, prior_signature in rows:
            if prior_key == event_key:
                return True
            prior_titles = [title.strip() for title in prior_signature.splitlines() if title.strip()]
            if any(titles_describe_same_event(a, b) for a in current_titles for b in prior_titles):
                return True
        return False

    def record(self, event_key: str, batch_id: str, *, signature: str = "", now: datetime | None = None) -> None:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT OR IGNORE INTO selected_events(event_key, signature, selected_at, batch_id) VALUES (?, ?, ?, ?)",
            (event_key, signature, timestamp, batch_id),
        )
        self.connection.commit()

    def prune(self, *, keep_days: int = 60, now: datetime | None = None) -> None:
        clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (clock - timedelta(days=keep_days)).isoformat()
        self.connection.execute("DELETE FROM selected_events WHERE selected_at < ?", (cutoff,))
        self.connection.commit()

    def completed_batch(self, batch_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT summary_json FROM completed_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def mark_batch_completed(self, batch_id: str, summary: dict, *, now: datetime | None = None) -> None:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO completed_batches(batch_id, completed_at, summary_json) VALUES (?, ?, ?) "
            "ON CONFLICT(batch_id) DO UPDATE SET completed_at=excluded.completed_at, summary_json=excluded.summary_json",
            (batch_id, timestamp, json.dumps(summary, ensure_ascii=False)),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
