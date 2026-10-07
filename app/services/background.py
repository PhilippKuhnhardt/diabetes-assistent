"""Background job: keep the local copy and the stored analysis current without an open app.

Every APP_SYNC_MINUTES (default 5, 0 switches it off) it mirrors yesterday and today from
Nightscout, then makes sure every day the analysis reads and the analysis page of every
period are stored. Opening the app or calling the read API then finds current results.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.ingestion import nightscout, sync
from app.services import day_view, summary
from app.store import config

PERIODS = (14, 30, 7, 3, 1)  # the analysis page's default first


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
    for offset in range(summary.history_days(max(PERIODS)) + 1):
        day_view.day_payload((today - timedelta(days=offset)).isoformat(), refresh=False)
    for days in PERIODS:
        summary.summary({"days": [str(days)]})
    return {"errors": errors}


def _loop(minutes: int) -> None:
    while True:
        started = time.monotonic()
        try:
            result = run_once()
            if result.get("errors"):
                # Only the days: error texts may carry Nightscout details.
                print("Hintergrund-Sync mit Fehlern für " + ", ".join(sorted(result["errors"])), flush=True)
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
