"""Time-zone switches while travelling (user request 2026-11-07): from each switch on, the app reads
local clock times in that zone; '' is the home zone (Nightscout profile / APP_TIMEZONE)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.store import db
from app.store.db import connect

# The switches are read on every local-time conversion; cached per database until a write.
_cache: dict = {}


def switches() -> tuple[tuple[datetime, str], ...]:
    """All switches, oldest first, as (instant UTC, zone name or '' for home)."""
    path = db.DB_PATH
    if path not in _cache:
        try:
            with connect() as conn:
                rows = conn.execute("SELECT start_at,zone FROM timezone_periods ORDER BY start_at,id").fetchall()
        except sqlite3.OperationalError:
            rows = []
        _cache.clear()
        _cache[path] = tuple((datetime.fromisoformat(row["start_at"]), row["zone"]) for row in rows)
    return _cache[path]


def forget() -> None:
    _cache.clear()


def add_switch(at: datetime, zone: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO timezone_periods(start_at,zone,created_at) VALUES(?,?,?)",
                     (at.astimezone(timezone.utc).isoformat(), zone, datetime.now(timezone.utc).isoformat()))
    forget()


def delete_last() -> bool:
    with connect() as conn:
        row = conn.execute("SELECT id FROM timezone_periods ORDER BY start_at DESC,id DESC LIMIT 1").fetchone()
        if row:
            conn.execute("DELETE FROM timezone_periods WHERE id=?", (row["id"],))
    forget()
    return bool(row)
