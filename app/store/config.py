"""Versioned local settings and the cached current Nightscout profile."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from app.store.db import connect


# Suggested glucose drop per sport type in mg/dL; negative values raise glucose
# (racing adrenaline).
DEFAULT_SPORT_EFFECTS = {"jogging": 40, "gym": 40, "light_cardio": 20, "racing": -40}


# Usual daily basal dose offered with breakfast (IE); configurable in the settings.
DEFAULT_BASAL_UNITS = "17"


def default_settings() -> dict:
    return {
        "timezone": os.environ.get("APP_TIMEZONE", "Europe/Berlin"),
        "window_minutes": 180,
        "dose_target_mgdl": 100,
        "dose_cautious_target_mgdl": 140,
        "auto_percentile": "37.5",
        "auto_lookback_days": 30,
        "auto_half_life_days": "7",
        "auto_method": "percentile",
        "auto_surcharge_mgdl": "40",
        "peak_minutes": 60,
        "duration_minutes": 180,
        "breakfast_end_minute": 660,
        "lunch_end_minute": 1020,
        "sport_lookback_minutes": 360,
        "sport_effects_mgdl": dict(DEFAULT_SPORT_EFFECTS),
        "basal_default_units": DEFAULT_BASAL_UNITS,
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
    result.setdefault("dose_cautious_target_mgdl", 140)
    result.setdefault("auto_percentile", "37.5")
    # Older versions stored methods that were removed (day shift, equal weights): back to the percentile.
    if result.get("auto_method") not in ("percentile", "surcharge"):
        result["auto_method"] = "percentile"
    # Lookbacks offered changed from 14/21/28 to 8/15/30 days: an old 14 becomes 15, anything else 30.
    if result.get("auto_lookback_days") not in (8, 15, 30):
        result["auto_lookback_days"] = 15 if result.get("auto_lookback_days") == 14 else 30
    result.setdefault("auto_half_life_days", "7")
    result.setdefault("auto_surcharge_mgdl", "40")
    result.setdefault("lunch_end_minute", 1020)
    result.setdefault("basal_default_units", DEFAULT_BASAL_UNITS)
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


def settings_history() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id, created_at, effective_at, payload FROM settings_versions ORDER BY id DESC").fetchall()
    return [{"id": row["id"], "created_at": row["created_at"], "effective_at": row["effective_at"], "settings": json.loads(row["payload"])} for row in rows]


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
