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


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_base_schema]


def init() -> None:
    with connect() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            migration(db)
            db.execute(f"PRAGMA user_version={number}")
