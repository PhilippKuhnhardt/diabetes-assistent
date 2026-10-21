"""SQLite connection and schema migrations.

Each migration runs once, in order; PRAGMA user_version stores how many have run.
New tables or columns are appended as a new migration, never by editing an old one.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("APP_DB", "data/app.sqlite3"))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        with db:
            yield db
    finally:
        db.close()


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    payload TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_at ON entries(at);
CREATE TABLE IF NOT EXISTS treatments (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    payload TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS treatments_at ON treatments(at);
CREATE TABLE IF NOT EXISTS synced_days (
    day TEXT PRIMARY KEY,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_exclusions (
    meal_id TEXT PRIMARY KEY,
    excluded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_day_types (
    day TEXT PRIMARY KEY,
    day_type TEXT NOT NULL CHECK(day_type IN ('work', 'free'))
);
CREATE TABLE IF NOT EXISTS vacation_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_day TEXT NOT NULL,
    end_day TEXT
);
CREATE TABLE IF NOT EXISTS analysis_sport_marks (
    meal_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('affected', 'unaffected')),
    effect_mgdl TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS analysis_windows (
    meal_id TEXT PRIMARY KEY,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nightscout_profiles (
    start_at TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    isf_schedule TEXT NOT NULL,
    ic_schedule TEXT NOT NULL DEFAULT '[]',
    synced_at TEXT NOT NULL,
    target_schedule TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS basal_doses (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    units TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    nightscout_synced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS basal_doses_at ON basal_doses(at);
CREATE TABLE IF NOT EXISTS nightscout_profile_sync (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    attempted_at TEXT NOT NULL,
    synced_at TEXT
);
"""


def _base_schema(db: sqlite3.Connection) -> None:
    """Tables up to October 2026. Databases created before migrations existed already have
    some of them, so this step only adds what is missing."""
    db.executescript(BASE_SCHEMA)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(nightscout_profiles)")}
    if "ic_schedule" not in columns:
        db.execute("ALTER TABLE nightscout_profiles ADD COLUMN ic_schedule TEXT NOT NULL DEFAULT '[]'")
    if "target_schedule" not in columns:
        db.execute("ALTER TABLE nightscout_profiles ADD COLUMN target_schedule TEXT NOT NULL DEFAULT '[]'")
    mark_columns = {row["name"] for row in db.execute("PRAGMA table_info(analysis_sport_marks)")}
    if "effect_mgdl" not in mark_columns:
        db.execute("ALTER TABLE analysis_sport_marks ADD COLUMN effect_mgdl TEXT NOT NULL DEFAULT '0'")


def _day_results(db: sqlite3.Connection) -> None:
    """Calculated diary days, reused while their inputs are unchanged."""
    db.execute("""CREATE TABLE IF NOT EXISTS day_results (
        day TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL,
        computed_at TEXT NOT NULL
    )""")


def _summary_results(db: sqlite3.Connection) -> None:
    """Analysis pages (factor groups, Automatik factors, backtest matrix) per period."""
    db.execute("""CREATE TABLE IF NOT EXISTS summary_results (
        key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL,
        computed_at TEXT NOT NULL
    )""")


def _notifications(db: sqlite3.Connection) -> None:
    """Push reminders: which kinds are switched on, and every message sent (one per key, never twice)."""
    db.execute("""CREATE TABLE IF NOT EXISTS notification_settings (
        kind TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS notifications_sent (
        key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        sent_at TEXT NOT NULL
    )""")


def _notification_priority(db: sqlite3.Connection) -> None:
    """Pushover priority per reminder kind (NULL = the kind's default)."""
    db.execute("ALTER TABLE notification_settings ADD COLUMN priority INTEGER")


def _places(db: sqlite3.Connection) -> None:
    """Eating places (chips) with their spots, and where each diary carb entry was eaten (by its
    event id); positions stay local."""
    db.execute("""CREATE TABLE IF NOT EXISTS places (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        quick INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS place_spots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        place_id INTEGER NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        created_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS entry_locations (
        event_id TEXT PRIMARY KEY,
        at TEXT NOT NULL,
        lat REAL,
        lon REAL,
        accuracy_m REAL,
        place_id INTEGER,
        updated_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS entry_locations_at ON entry_locations(at)")


def _inbox_seen(db: sqlite3.Connection) -> None:
    """Hints on the diary's start page that were opened once, so they lose their highlight."""
    db.execute("""CREATE TABLE IF NOT EXISTS inbox_seen (
        key TEXT PRIMARY KEY,
        seen_at TEXT NOT NULL
    )""")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_base_schema, _day_results, _summary_results,
                                                          _notifications, _notification_priority, _places,
                                                          _inbox_seen]


def init() -> None:
    with connect() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            migration(db)
            db.execute(f"PRAGMA user_version={number}")
