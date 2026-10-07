"""Live Nightscout reads: profile cache, day mirror, current glucose."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import episodes, glucose, nightscout, storage
from app.common import local_day_range, parse_instant



def sync_profile_cache(force: bool = False) -> None:
    if not nightscout.is_configured():
        raise nightscout.NightscoutError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    if not force and not storage.profile_sync_due():
        return
    storage.mark_profile_sync_attempt()
    profiles = nightscout.fetch_profiles()
    storage.mirror_nightscout_profiles(profiles)


def sync_days(payload: dict) -> dict:
    if not nightscout.is_configured():
        raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    days = int(payload.get("days", 14))
    if days not in (1, 3, 7, 14, 30):
        raise ValueError("Ungültiger Zeitraum")
    profile_sync_error = None
    try:
        sync_profile_cache(force=True)
    except nightscout.NightscoutError as error:
        profile_sync_error = str(error)
    _, settings = storage.get_settings()
    today = datetime.now(ZoneInfo(settings["timezone"])).date()
    errors = []
    synced = 0
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        start, end = local_day_range(day, settings["timezone"])
        try:
            entries, treatments = nightscout.fetch_range(start, end)
            storage.mirror_day(day, entries, treatments, start, end)
            synced += 1
        except nightscout.NightscoutError as error:
            errors.append({"day": day, "error": str(error)})
    return {"synced": synced, "errors": errors, "profile_sync_error": profile_sync_error}


def sync_profile(payload: dict) -> dict:
    sync_profile_cache(force=True)
    version, settings = storage.get_settings()
    profile_sync = storage.profile_sync_status()
    profile = settings.get("nightscout_profile")
    return {
        "version": version, "settings": settings, "profile": profile,
        "profile_sync_at": profile_sync["synced_at"],
        "message": ("Nightscout-Profil synchronisiert" if profile else
                    "Nightscout hat kein Korrekturfaktor-Profil geliefert"),
    }


def test_connection() -> dict:
    if not nightscout.is_configured():
        raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    sync_profile_cache(force=True)
    _, settings = storage.get_settings()
    today = datetime.now(ZoneInfo(settings["timezone"])).date().isoformat()
    start, end = local_day_range(today, settings["timezone"])
    nightscout.fetch_range(start, end)
    return {"message": "Nightscout-Verbindung und aktuelles Profil erfolgreich geprüft"}


def current_glucose() -> dict:
    if not nightscout.is_configured():
        raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    profile_current = True
    try:
        sync_profile_cache()
    except nightscout.NightscoutError:
        profile_current = False
    _, settings = storage.get_settings()
    profile_sync = storage.profile_sync_status()
    synced_at = profile_sync["synced_at"]
    attempted_at = profile_sync["attempted_at"]
    profile_current = (profile_current and bool(synced_at) and
                       (not attempted_at or parse_instant(synced_at) >= parse_instant(attempted_at)))
    if not profile_current:
        settings["target_schedule"] = []
    now = datetime.now(timezone.utc)
    raw = nightscout.fetch_recent_glucose(now)
    entries = episodes.normalize_entries(raw)
    return glucose.current_status(entries, settings, now)
