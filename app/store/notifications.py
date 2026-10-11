"""Push reminders: on/off and Pushover priority per kind, and the messages already sent."""
from __future__ import annotations

from datetime import datetime, timezone

from app.store.db import connect


def enabled_kinds(defaults: dict[str, bool]) -> dict[str, bool]:
    with connect() as db:
        rows = db.execute("SELECT kind,enabled FROM notification_settings").fetchall()
    stored = {row["kind"]: bool(row["enabled"]) for row in rows}
    return {kind: stored.get(kind, default) for kind, default in defaults.items()}


def set_enabled(kind: str, enabled: bool) -> None:
    with connect() as db:
        db.execute("INSERT INTO notification_settings(kind,enabled) VALUES(?,?) "
                   "ON CONFLICT(kind) DO UPDATE SET enabled=excluded.enabled", (kind, int(enabled)))


def priorities(defaults: dict[str, int]) -> dict[str, int]:
    with connect() as db:
        rows = db.execute("SELECT kind,priority FROM notification_settings WHERE priority IS NOT NULL").fetchall()
    stored = {row["kind"]: int(row["priority"]) for row in rows}
    return {kind: stored.get(kind, default) for kind, default in defaults.items()}


def set_priority(kind: str, priority: int, enabled_default: bool = True) -> None:
    """Store a kind's Pushover priority; a kind never switched keeps its default on/off state."""
    with connect() as db:
        db.execute("INSERT INTO notification_settings(kind,enabled,priority) VALUES(?,?,?) "
                   "ON CONFLICT(kind) DO UPDATE SET priority=excluded.priority", (kind, int(enabled_default), priority))


def last_sent(kind: str) -> datetime | None:
    with connect() as db:
        row = db.execute("SELECT MAX(sent_at) AS at FROM notifications_sent WHERE kind=?", (kind,)).fetchone()
    return datetime.fromisoformat(row["at"]) if row and row["at"] else None


def was_sent(key: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM notifications_sent WHERE key=?", (key,)).fetchone() is not None


def mark_sent(key: str, kind: str, title: str, message: str, at: datetime | None = None) -> None:
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO notifications_sent(key,kind,title,message,sent_at) VALUES(?,?,?,?,?)",
                   (key, kind, title, message, (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()))


def recent(limit: int = 5) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT kind,title,message,sent_at FROM notifications_sent ORDER BY sent_at DESC LIMIT ?",
                          (limit,)).fetchall()
    return [dict(row) for row in rows]
