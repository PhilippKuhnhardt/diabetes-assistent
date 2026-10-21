"""Hints on the diary's start page that Philipp has already opened (by the hint's key)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.store.db import connect


def seen_keys(keys: list[str]) -> set[str]:
    if not keys:
        return set()
    with connect() as db:
        rows = db.execute(f"SELECT key FROM inbox_seen WHERE key IN ({','.join('?' * len(keys))})", keys).fetchall()
    return {row["key"] for row in rows}


def mark_seen(key: str) -> None:
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO inbox_seen(key,seen_at) VALUES(?,?)",
                   (key, datetime.now(timezone.utc).isoformat()))
