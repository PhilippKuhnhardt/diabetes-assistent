"""Background job: keep the local copy and the stored analysis current without an open app.

Every APP_SYNC_MINUTES (default 5, 0 switches it off) it mirrors yesterday and today from
Nightscout and checks the reminders. Only when yesterday or today read something new does it
make sure every day the analysis reads and the analysis page of every period are stored.
Opening the app or calling the read API then finds current results.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.ingestion import nightscout, pushover, sync
from app.services import day_view, notifications, summary
from app.store import config

PERIODS = (14, 30, 7, 3, 1)  # the analysis page's default first
# What the last precompute saw; while it is unchanged a round only checks the reminders.
_last_inputs: tuple | None = None


def interval_minutes() -> int:
    value = os.environ.get("APP_SYNC_MINUTES", "5")
    try:
        minutes = int(value)
    except ValueError:
        raise SystemExit("APP_SYNC_MINUTES muss eine ganze Minutenzahl sein") from None
    if not 0 <= minutes <= 1440:
        raise SystemExit("APP_SYNC_MINUTES muss zwischen 0 und 1440 liegen")
    return minutes


def run_once() -> dict:
    """One round: mirror, then precompute. Returns the sync errors of the round."""
    if not nightscout.is_configured():
        return {"skipped": "Nightscout ist nicht verbunden"}
    _, settings = config.get_settings()
    today = datetime.now(ZoneInfo(settings["timezone"])).date()
    errors = {}
    for day in (today - timedelta(days=1), today):
        status = sync.sync_day(day.isoformat())
        failure = status["sync_error"] or status["basal_sync_error"]
        if failure:
            errors[day.isoformat()] = failure
    result = {"errors": errors, "computed": _precompute(today)}
    # Reminders only read a freshly mirrored today, so an injected dose cannot be reminded again.
    if today.isoformat() not in errors:
        try:
            sent = notifications.check()
            if sent:
                result["notified"] = [item["kind"] for item in sent]
        except pushover.PushoverError as error:
            result["notify_error"] = str(error)
    if not errors and "notify_error" not in result:
        pushover.heartbeat()
    return result


def _precompute(today) -> bool:
    """Store every day the analysis reads and the analysis page of every period, but only when
    yesterday or today read something new. Their fingerprints also cover the settings, profile,
    marks and code, so an unchanged pair means the stored results are still current."""
    global _last_inputs
    inputs = (today.isoformat(), *(day_view.day_fingerprint((today - timedelta(days=offset)).isoformat())
                                   for offset in (1, 0)))
    if inputs == _last_inputs:
        return False
    for offset in range(summary.history_days(max(PERIODS)) + 1):
        day_view.day_payload((today - timedelta(days=offset)).isoformat(), refresh=False)
    for days in PERIODS:
        summary.summary({"days": [str(days)]})
    _last_inputs = inputs
    return True


def _loop(minutes: int) -> None:
    while True:
        started = time.monotonic()
        try:
            result = run_once()
            if result.get("errors"):
                # Only the days: error texts may carry Nightscout details.
                print("Hintergrund-Sync mit Fehlern für " + ", ".join(sorted(result["errors"])), flush=True)
            if result.get("notify_error"):
                print("Benachrichtigung fehlgeschlagen: " + result["notify_error"], flush=True)
        except Exception as error:  # keep the job alive; the next round retries
            print(f"Hintergrund-Sync fehlgeschlagen: {type(error).__name__}", flush=True)
        time.sleep(max(1.0, minutes * 60 - (time.monotonic() - started)))


def start() -> threading.Thread | None:
    minutes = interval_minutes()
    if not minutes:
        return None
    thread = threading.Thread(target=_loop, args=(minutes,), name="background-sync", daemon=True)
    thread.start()
    return thread
