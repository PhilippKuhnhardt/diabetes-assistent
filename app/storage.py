"""SQLite mirror of Nightscout data and versioned application settings."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
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


def init() -> None:
    with connect() as db:
        db.executescript("""
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
        CREATE TABLE IF NOT EXISTS nightscout_profile_sync (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            attempted_at TEXT NOT NULL,
            synced_at TEXT
        );
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(nightscout_profiles)")}
        if "ic_schedule" not in columns:
            db.execute("ALTER TABLE nightscout_profiles ADD COLUMN ic_schedule TEXT NOT NULL DEFAULT '[]'")
        if "target_schedule" not in columns:
            db.execute("ALTER TABLE nightscout_profiles ADD COLUMN target_schedule TEXT NOT NULL DEFAULT '[]'")
        mark_columns = {row["name"] for row in db.execute("PRAGMA table_info(analysis_sport_marks)")}
        if "effect_mgdl" not in mark_columns:
            db.execute("ALTER TABLE analysis_sport_marks ADD COLUMN effect_mgdl TEXT NOT NULL DEFAULT '0'")


# Suggested glucose drop per sport type in mg/dL; negative values raise glucose
# (racing adrenaline).
DEFAULT_SPORT_EFFECTS = {"jogging": 40, "gym": 40, "light_cardio": 20, "racing": -40}


def default_settings() -> dict:
    return {
        "timezone": os.environ.get("APP_TIMEZONE", "Europe/Berlin"),
        "window_minutes": 180,
        "dose_target_mgdl": 100,
        "peak_minutes": 60,
        "duration_minutes": 180,
        "breakfast_end_minute": 660,
        "lunch_end_minute": 1020,
        "sport_lookback_minutes": 360,
        "sport_effects_mgdl": dict(DEFAULT_SPORT_EFFECTS),
    }


def current_nightscout_profile() -> dict | None:
    """Return the one cached Nightscout profile used for all local analyses."""
    with connect() as db:
        row = db.execute(
            "SELECT start_at,profile_id,profile_name,timezone,isf_schedule,ic_schedule,target_schedule,synced_at "
            "FROM nightscout_profiles ORDER BY start_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "start_at": row["start_at"], "profile_id": row["profile_id"],
        "profile_name": row["profile_name"], "timezone": row["timezone"],
        "isf_schedule": json.loads(row["isf_schedule"]),
        "ic_schedule": json.loads(row["ic_schedule"]),
        "target_schedule": json.loads(row["target_schedule"]), "synced_at": row["synced_at"],
    }


def _with_nightscout_profile(settings: dict) -> dict:
    """Overlay Nightscout's current profile, including for older meal analyses."""
    result = dict(settings)
    profile = current_nightscout_profile()
    result["isf_schedule"] = profile["isf_schedule"] if profile else []
    result["ic_schedule"] = profile["ic_schedule"] if profile else []
    result["target_schedule"] = profile["target_schedule"] if profile else []
    result.setdefault("breakfast_end_minute", 660)
    result.setdefault("dose_target_mgdl", 100)
    result.setdefault("lunch_end_minute", 1020)
    result.setdefault("sport_lookback_minutes", 360)
    # Older settings versions predate newer sport types such as racing.
    result["sport_effects_mgdl"] = {**DEFAULT_SPORT_EFFECTS, **result.get("sport_effects_mgdl", {})}
    result["nightscout_profile"] = ({
        "start_at": profile["start_at"], "profile_id": profile["profile_id"],
        "profile_name": profile["profile_name"], "timezone": profile["timezone"],
    } if profile else None)
    if profile:
        result["timezone"] = profile["timezone"]
    return result


def get_settings() -> tuple[int, dict]:
    with connect() as db:
        row = db.execute("SELECT id, payload FROM settings_versions ORDER BY id DESC LIMIT 1").fetchone()
    version, settings = (row["id"], json.loads(row["payload"])) if row else (0, default_settings())
    return version, _with_nightscout_profile(settings)


def save_settings(payload: dict, effective_at: datetime | None = None) -> int:
    # ISF values are always read from Nightscout; never copy them into a local settings version.
    stored = dict(payload)
    stored.pop("isf_schedule", None)
    stored.pop("ic_schedule", None)
    stored.pop("target_schedule", None)
    stored.pop("nightscout_profile", None)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        existing = db.execute("SELECT COUNT(*) FROM settings_versions").fetchone()[0]
        effective = (effective_at.astimezone(timezone.utc).isoformat() if effective_at else
                     now if existing else "1970-01-01T00:00:00+00:00")
        cursor = db.execute(
            "INSERT INTO settings_versions(created_at,effective_at,payload) VALUES(?,?,?)",
            (now, effective, json.dumps(stored, ensure_ascii=False, separators=(",", ":"))),
        )
        return int(cursor.lastrowid)


def get_settings_at(moment: datetime) -> tuple[int, dict]:
    with connect() as db:
        row = db.execute(
            "SELECT id, payload FROM settings_versions WHERE effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1",
            (moment.astimezone(timezone.utc).isoformat(),),
        ).fetchone()
    if row:
        version, settings = row["id"], json.loads(row["payload"])
    else:
        version, settings = get_settings()
    return version, _with_nightscout_profile(settings)


def profile_sync_due(cooldown_minutes: int = 5) -> bool:
    with connect() as db:
        row = db.execute("SELECT attempted_at FROM nightscout_profile_sync WHERE id=1").fetchone()
    if not row:
        return True
    attempted = datetime.fromisoformat(row["attempted_at"].replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - attempted.astimezone(timezone.utc)
    return age.total_seconds() >= cooldown_minutes * 60


def mark_profile_sync_attempt() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute(
            "INSERT INTO nightscout_profile_sync(id,attempted_at,synced_at) VALUES(1,?,NULL) "
            "ON CONFLICT(id) DO UPDATE SET attempted_at=excluded.attempted_at",
            (now,),
        )


def profile_sync_status() -> dict:
    with connect() as db:
        row = db.execute("SELECT attempted_at,synced_at FROM nightscout_profile_sync WHERE id=1").fetchone()
    return {"attempted_at": row["attempted_at"], "synced_at": row["synced_at"]} if row else {
        "attempted_at": None, "synced_at": None,
    }


def mirror_nightscout_profiles(profiles: list[dict]) -> None:
    """Replace the local profile cache with Nightscout's current profile."""
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("DELETE FROM nightscout_profiles")
        for profile in profiles:
            db.execute(
                "INSERT INTO nightscout_profiles(start_at,profile_id,profile_name,timezone,isf_schedule,ic_schedule,target_schedule,synced_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (profile["start_at"], profile["profile_id"], profile["profile_name"], profile["timezone"],
                 json.dumps(profile["isf_schedule"], separators=(",", ":")),
                 json.dumps(profile["ic_schedule"], separators=(",", ":")),
                 json.dumps(profile.get("target_schedule", []), separators=(",", ":")), now),
            )
        db.execute(
            "INSERT INTO nightscout_profile_sync(id,attempted_at,synced_at) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET attempted_at=excluded.attempted_at,synced_at=excluded.synced_at",
            (now, now),
        )

def settings_history() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id, created_at, effective_at, payload FROM settings_versions ORDER BY id DESC").fetchall()
    return [{"id": row["id"], "created_at": row["created_at"], "effective_at": row["effective_at"], "settings": json.loads(row["payload"])} for row in rows]


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


def delete_cached_treatment(record_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM treatments WHERE id=?", (record_id,))


def synced_at(day: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT synced_at FROM synced_days WHERE day=?", (day,)).fetchone()
    return row["synced_at"] if row else None



def analysis_exclusions() -> set[str]:
    with connect() as db:
        rows = db.execute("SELECT meal_id FROM analysis_exclusions").fetchall()
    return {row["meal_id"] for row in rows}


def set_analysis_exclusion(meal_id: str, excluded: bool) -> None:
    with connect() as db:
        if excluded:
            db.execute(
                "INSERT INTO analysis_exclusions(meal_id,excluded_at) VALUES(?,?) "
                "ON CONFLICT(meal_id) DO NOTHING",
                (meal_id, datetime.now(timezone.utc).isoformat()),
            )
        else:
            db.execute("DELETE FROM analysis_exclusions WHERE meal_id=?", (meal_id,))


def analysis_day_types() -> dict[str, str]:
    with connect() as db:
        rows = db.execute("SELECT day, day_type FROM analysis_day_types").fetchall()
    return {row["day"]: row["day_type"] for row in rows}


def set_analysis_day_type(day: str, day_type: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO analysis_day_types(day,day_type) VALUES(?,?) "
            "ON CONFLICT(day) DO UPDATE SET day_type=excluded.day_type",
            (day, day_type),
        )


def analysis_sport_marks() -> dict[str, dict]:
    with connect() as db:
        rows = db.execute("SELECT meal_id,status,effect_mgdl FROM analysis_sport_marks").fetchall()
    return {row["meal_id"]: {"status": row["status"], "effect_mgdl": row["effect_mgdl"]}
            for row in rows}


def set_analysis_sport_mark(meal_id: str, status: str, effect_mgdl: str = "0") -> None:
    with connect() as db:
        if status == "auto":
            db.execute("DELETE FROM analysis_sport_marks WHERE meal_id=?", (meal_id,))
        else:
            db.execute("INSERT INTO analysis_sport_marks(meal_id,status,effect_mgdl) VALUES(?,?,?) "
                       "ON CONFLICT(meal_id) DO UPDATE SET status=excluded.status,effect_mgdl=excluded.effect_mgdl",
                       (meal_id, status, effect_mgdl))


def analysis_windows() -> dict[str, dict]:
    """Manually chosen meal windows; local analysis cleanup, never sent to Nightscout."""
    with connect() as db:
        rows = db.execute("SELECT meal_id,start_at,end_at FROM analysis_windows").fetchall()
    return {row["meal_id"]: {"start": datetime.fromisoformat(row["start_at"]),
                             "end": datetime.fromisoformat(row["end_at"])}
            for row in rows}


def set_analysis_window(meal_id: str, start: datetime | None, end: datetime | None) -> None:
    with connect() as db:
        if start is None or end is None:
            db.execute("DELETE FROM analysis_windows WHERE meal_id=?", (meal_id,))
        else:
            db.execute(
                "INSERT INTO analysis_windows(meal_id,start_at,end_at,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(meal_id) DO UPDATE SET start_at=excluded.start_at,end_at=excluded.end_at,"
                "updated_at=excluded.updated_at",
                (meal_id, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
                 datetime.now(timezone.utc).isoformat()),
            )
