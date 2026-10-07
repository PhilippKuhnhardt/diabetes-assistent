"""Local mirror of Nightscout CGM readings and treatments, per synced day."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.store.db import connect


def _utc_iso(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def mirror_day(day: str, entries: list[dict], treatments: list[dict], start: datetime, end: datetime) -> None:
    """Replace authoritative records in the fetched range; preserve other days."""
    now = datetime.now(timezone.utc).isoformat()
    lo, hi = start.isoformat(), end.isoformat()
    with connect() as db:
        db.execute("DELETE FROM entries WHERE at>=? AND at<?", (lo, hi))
        db.execute("DELETE FROM treatments WHERE at>=? AND at<?", (lo, hi))
        for entry in entries:
            at = _utc_iso(entry["dateString"]) if entry.get("dateString") else datetime.fromtimestamp(entry["date"] / 1000, timezone.utc).isoformat()
            key = str(entry.get("_id") or f"entry:{entry.get('date')}:{entry.get('type')}")
            db.execute("INSERT OR REPLACE INTO entries(id,at,payload,synced_at) VALUES(?,?,?,?)", (key, at, json.dumps(entry), now))
        for treatment in treatments:
            at = _utc_iso(treatment["created_at"]) if treatment.get("created_at") else datetime.fromtimestamp(treatment["date"] / 1000, timezone.utc).isoformat()
            key = str(treatment.get("_id") or treatment.get("identifier") or f"treatment:{at}:{treatment.get('eventType')}")
            db.execute("INSERT OR REPLACE INTO treatments(id,at,payload,synced_at) VALUES(?,?,?,?)", (key, at, json.dumps(treatment), now))
        db.execute("INSERT INTO synced_days(day,synced_at) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET synced_at=excluded.synced_at", (day, now))


def cached_between(start: datetime, end: datetime) -> tuple[list[dict], list[dict]]:
    lo, hi = start.isoformat(), end.isoformat()
    with connect() as db:
        entries = db.execute("SELECT payload FROM entries WHERE at>=? AND at<? ORDER BY at", (lo, hi)).fetchall()
        treatments = db.execute("SELECT payload FROM treatments WHERE at>=? AND at<? ORDER BY at", (lo, hi)).fetchall()
    return [json.loads(row["payload"]) for row in entries], [json.loads(row["payload"]) for row in treatments]


def first_cached_at() -> str | None:
    """Earliest mirrored CGM reading, treatment or local basal dose (UTC ISO), or None for an empty mirror."""
    with connect() as db:
        row = db.execute("SELECT MIN(at) AS at FROM (SELECT MIN(at) AS at FROM entries "
                         "UNION ALL SELECT MIN(at) FROM treatments UNION ALL SELECT MIN(at) FROM basal_doses)").fetchone()
    return row["at"] if row else None


def delete_cached_treatment(record_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM treatments WHERE id=?", (record_id,))


def synced_at(day: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT synced_at FROM synced_days WHERE day=?", (day,)).fetchone()
    return row["synced_at"] if row else None
