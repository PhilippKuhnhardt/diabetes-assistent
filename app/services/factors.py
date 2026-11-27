"""Calculated meal factors over past days, as the dose help and analysis use them."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app import core
from app.services import day_view
from app.store import config, marks
from app.store import places as place_store



def counted_days(today: date, days_back: int, periods: list[dict], mode: str) -> list[tuple[date, date]]:
    """(day, day as the Automatik counts it) from today back over days_back days, newest first.

    Vacation meals: outside a vacation, modes
    "separate" and "vacation_only" skip vacation days and move the days before them up by the
    vacation's length, so after coming home the lookback and the half-life reach the meals before
    the vacation as if it had not happened, and new meals take over as usual. During a vacation
    "separate" counts every day as before; "vacation_only" counts only vacation days. "all" counts
    every day everywhere. (The rule: services.rs counted_days.)"""
    return core.call("factors.counted_days", today=today, days_back=days_back, periods=periods, mode=mode)


def vacation_mode(settings: dict) -> str:
    return settings.get("auto_vacation", config.DEFAULT_AUTO_VACATION)


def meal_points(today: date, days_back: int, overrides: dict[str, str], periods: list[dict],
                loaded: dict[str, dict] | None = None, mode: str = config.DEFAULT_AUTO_VACATION) -> list[dict]:
    """Valid, non-excluded meal factors from today back to days_back days, for the suggestion.
    Breakfast is keyed by the day type of its day. `day` is the day as the Automatik counts it
    (counted_days: vacation days skipped after a vacation), `real_day` the calendar day.
    (The rule: summary.rs meal_points; this reads the stored days it needs.)"""
    payloads = {}
    for real_day, _ in counted_days(today, days_back, periods, mode):
        text = real_day.isoformat()
        payload = loaded[text] if loaded is not None and text in loaded else day_view.day_payload(text, refresh=False)
        if loaded is not None:
            loaded[text] = payload
        payloads[text] = {"meals": [{key: meal.get(key) for key in ("id", "meal", "at", "calculation", "quality",
                                                                     "fpe_surcharge", "place")}
                                    for meal in payload.get("meals", [])]}
    return core.call("factors.meal_points", today=today, days_back=days_back, mode=mode, payloads=payloads,
                     analysis_exclusions=sorted(marks.analysis_exclusions()),
                     places=[{"id": place["id"], "adjust": bool(place.get("adjust"))} for place in place_store.all_places()],
                     day_types=overrides, vacation_periods=periods)


def meal_surcharge_mgdl(settings: dict, fat: bool) -> Decimal | None:
    """Surcharge of one main meal as the dose help and "Wirkt noch" use it; None without the surcharge method."""
    return core.call("factors.meal_surcharge_mgdl", settings=settings, fat=fat)


def place_scales(now: datetime) -> dict[int, dict]:
    """The place factor of every place switched on for it (forecast.place_scale), by place id:
    count, min_meals, days and scale (None below min_meals). Reads the dose help's snapshot."""
    from app.services import dose_help
    return {int(place_id): item for place_id, item in core.call("factors.place_scales", **dose_help.snapshot({}, now)).items()}
