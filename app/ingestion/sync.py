"""Sync steps that copy Nightscout into SQLite. Analysis and views read only the local copy."""
from __future__ import annotations

from app.common import local_day_range
from app.ingestion import basal_sync, nightscout
from app.store import config, mirror


# Per local day: the Nightscout write count when it was last mirrored in this process.
_mirrored: dict[str, int] = {}


def mirrored_after_writes(day: str) -> bool:
    """Whether the day was mirrored in this process after its last write to Nightscout."""
    return _mirrored.get(day) == nightscout.write_count()


def sync_profile_cache(force: bool = False) -> None:
    if not nightscout.is_configured():
        raise nightscout.NightscoutError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    if not force and not config.profile_sync_due():
        return
    config.mark_profile_sync_attempt()
    profiles = nightscout.fetch_profiles()
    config.mirror_nightscout_profiles(profiles)


def sync_day(day: str) -> dict:
    """Refresh the profile cache (with cooldown), mirror one local day and reconcile its basal doses.
    Errors are returned, not raised, so the day can still be shown from the local copy."""
    status = {"profile_sync_error": None, "sync_error": None, "basal_sync_error": None, "fresh": False}
    if not nightscout.is_configured():
        return status
    try:
        sync_profile_cache()
    except nightscout.NightscoutError as error:
        status["profile_sync_error"] = str(error)
    _, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    writes = nightscout.write_count()
    try:
        raw_entries, raw_treatments = nightscout.fetch_range(start, end)
        mirror.mirror_day(day, raw_entries, raw_treatments, start, end)
        _mirrored[day] = writes
    except nightscout.NightscoutError as error:
        status["sync_error"] = str(error)
        return status
    status["fresh"] = True
    _, cached_treatments = mirror.cached_between(start, end)
    _, status["basal_sync_error"] = basal_sync.reconcile_basal(start, end, cached_treatments, True)
    return status


def mirror_days(days: list[str], timezone_name: str) -> tuple[int, list[dict]]:
    """Mirror several local days; returns how many succeeded and the errors of the others."""
    errors = []
    synced = 0
    for day in days:
        start, end = local_day_range(day, timezone_name)
        writes = nightscout.write_count()
        try:
            entries, treatments = nightscout.fetch_range(start, end)
            mirror.mirror_day(day, entries, treatments, start, end)
            _mirrored[day] = writes
            synced += 1
        except nightscout.NightscoutError as error:
            errors.append({"day": day, "error": str(error)})
    return synced, errors
