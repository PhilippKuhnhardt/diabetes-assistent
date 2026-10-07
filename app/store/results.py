"""Stored analysis results. A result is valid only for the input fingerprint it was computed from."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.store.db import connect


def day_result(day: str, fingerprint: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT payload FROM day_results WHERE day=? AND fingerprint=?", (day, fingerprint)).fetchone()
    return json.loads(row["payload"]) if row else None


def save_day_result(day: str, fingerprint: str, payload: dict) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO day_results(day,fingerprint,payload,computed_at) VALUES(?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload,"
            "computed_at=excluded.computed_at",
            (day, fingerprint, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
             datetime.now(timezone.utc).isoformat()))


def day_inputs(start: datetime, end: datetime, day: str) -> list:
    """Everything a calculated day reads from SQLite, in a compact comparable form.
    Mirror rows are only ever rewritten with a new synced_at or deleted, so count, newest
    sync time and total size identify them; the small local tables are taken in full."""
    lo, hi = start.isoformat(), end.isoformat()
    with connect() as db:
        rows = [
            tuple(db.execute(f"SELECT COUNT(*), MAX(synced_at), SUM(LENGTH(payload)), MAX(id), MIN(id) FROM {table} "
                             "WHERE at>=? AND at<?", (lo, hi)).fetchone())
            for table in ("entries", "treatments")]
        queries = [
            ("SELECT id,at,units,notes,nightscout_synced FROM basal_doses WHERE at>=? AND at<? ORDER BY id", (lo, hi)),
            ("SELECT synced_at FROM synced_days WHERE day=?", (day,)),
            ("SELECT id,effective_at FROM settings_versions ORDER BY id", ()),
            ("SELECT start_at,profile_id,profile_name,timezone,isf_schedule,ic_schedule,target_schedule "
             "FROM nightscout_profiles ORDER BY start_at", ()),
            ("SELECT meal_id,status,effect_mgdl FROM analysis_sport_marks ORDER BY meal_id", ()),
            ("SELECT meal_id,start_at,end_at FROM analysis_windows ORDER BY meal_id", ()),
            ("SELECT day,day_type FROM analysis_day_types ORDER BY day", ()),
            ("SELECT start_day,end_day FROM vacation_periods ORDER BY id", ()),
        ]
        rows += [[tuple(row) for row in db.execute(sql, args).fetchall()] for sql, args in queries]
    return rows
