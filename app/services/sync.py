"""Endpoints that talk to Nightscout directly: manual sync, profile sync, connection test, current glucose."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import episodes, glucose
from app.common import local_day_range, parse_instant
from app.ingestion import nightscout, sync as ingestion_sync
from app.store import config



def sync_days(payload: dict) -> dict:
    if not nightscout.is_configured():
        raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    days = int(payload.get("days", 14))
    if days not in (1, 3, 7, 14, 30):
        raise ValueError("Ungültiger Zeitraum")
    profile_sync_error = None
    try:
        ingestion_sync.sync_profile_cache(force=True)
    except nightscout.NightscoutError as error:
        profile_sync_error = str(error)
    _, settings = config.get_settings()
    today = datetime.now(ZoneInfo(settings["timezone"])).date()
    synced, errors = ingestion_sync.mirror_days(
        [(today - timedelta(days=offset)).isoformat() for offset in range(days)], settings["timezone"])
    return {"synced": synced, "errors": errors, "profile_sync_error": profile_sync_error}


def sync_profile(payload: dict) -> dict:
    ingestion_sync.sync_profile_cache(force=True)
    version, settings = config.get_settings()
    profile_sync = config.profile_sync_status()
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
    ingestion_sync.sync_profile_cache(force=True)
    _, settings = config.get_settings()
    today = datetime.now(ZoneInfo(settings["timezone"])).date().isoformat()
    start, end = local_day_range(today, settings["timezone"])
    nightscout.fetch_range(start, end)
    return {"message": "Nightscout-Verbindung und aktuelles Profil erfolgreich geprüft"}


def current_glucose() -> dict:
    if not nightscout.is_configured():
        raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    profile_current = True
    try:
        ingestion_sync.sync_profile_cache()
    except nightscout.NightscoutError:
        profile_current = False
    _, settings = config.get_settings()
    profile_sync = config.profile_sync_status()
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
