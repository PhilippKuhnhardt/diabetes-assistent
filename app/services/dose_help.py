"""Dose suggestion for an entry being typed today.

The whole calculation runs in the Rust core (services.dose_help, core/diabetes-core/src/services.rs);
this module only gathers what it reads from the local store into one snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import core
from app.common import local_zone
from app.services import day_view, factors
from app.store import config, marks
from app.store import places as place_store

# Days of meal factors the snapshot carries: the Automatik's lookback plus the place factor's 60 days,
# and at least the breakfast day-type median's 28 days.
PLACE_DAYS, MIN_DAYS, MARGIN_DAYS = 60, 28, 5


def snapshot(payload: dict, now: datetime) -> dict:
    """Everything services.dose_help reads, from the local copy only."""
    _, settings = config.get_settings()
    today = now.astimezone(local_zone(settings["timezone"])).date()
    # A rolling span, not the calendar day: just after midnight last evening's doses, carbs and split still act.
    current = day_view.recent(now, refresh=not day_view.mirror_current(today.isoformat()))
    overrides, periods = marks.analysis_day_types(), marks.vacation_periods()
    mode = factors.vacation_mode(settings)
    back = max(int(settings["auto_lookback_days"]) + PLACE_DAYS, MIN_DAYS) + MARGIN_DAYS
    return {
        "now": now, "settings": settings, "payload": payload,
        "entries": current["entries"], "treatments": current["treatments"],
        "day_types": overrides, "vacation_periods": periods,
        "meal_points": factors.meal_points(today, back, overrides, periods, mode=mode),
        "places": place_store.all_places(), "entry_places": place_store.entry_places(),
        "sport_overrides": marks.sport_session_effects(), "sport_minutes_overrides": marks.sport_session_minutes(),
        "auto_vacation": mode,
    }


def dose_help(payload: dict) -> dict:
    return core.call("services.dose_help", **snapshot(payload, datetime.now(timezone.utc)))
