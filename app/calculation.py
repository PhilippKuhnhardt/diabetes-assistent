"""Deterministic, auditable meal-endpoint calculation.

This is a mathematical model, not an insulin dosing algorithm. Complete carb
absorption and full dose effect at the endpoint are explicit assumptions.
"""
from __future__ import annotations

from app import core
from bisect import bisect_left
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from app import insulin
from app.common import local_zone
from app.i18n import tr


getcontext().prec = 28


def dec(value: object) -> Decimal:
    return Decimal(str(value))


# Activity curve of a rapid dose: the active profile of app/insulin.py (APP_INSULIN, default Fiasp).
INSULIN_PEAK = insulin.ACTIVE.peak_minutes
INSULIN_DURATION = insulin.ACTIVE.duration_minutes
INSULIN_DELAY = insulin.ACTIVE.delay_minutes


def insulin_remaining(minutes: Decimal) -> Decimal:
    """Share of a rapid dose that has not acted yet, `minutes` after the injection."""
    return core.call("calculation.insulin_remaining", minutes=minutes)


def schedule_value_at(moment: datetime, schedule: list[dict], timezone: str, key: str) -> Decimal:
    return core.call(
        "calculation.schedule_value_at",
        moment=moment, schedule=schedule, timezone=timezone, key=key)


def isf_at(moment: datetime, schedule: list[dict], timezone: str) -> Decimal:
    return core.call("calculation.isf_at", moment=moment, schedule=schedule, timezone=timezone)


def minutes_between(later: datetime, earlier: datetime) -> Decimal:
    return core.call("calculation.minutes_between", later=later, earlier=earlier)


def is_rapid_insulin(treatment: dict) -> bool:
    return (treatment.get("role") in ("meal", "meal-dose", "correction")
            or treatment.get("eventType") in ("Meal Bolus", "Correction Bolus", "Snack Bolus", "Bolus"))


def interpolate_glucose(entries: list[dict], moment: datetime, max_gap_minutes: int = 15) -> dict:
    """Piecewise linear interpolation; no silent nearest-reading substitution."""
    return core.call(
        "calculation.interpolate_glucose",
        entries=entries, moment=moment, max_gap_minutes=max_gap_minutes)


def glucose_interpolator(entries: list[dict], max_gap_minutes: int = 15):
    """interpolate_glucose for many moments over the same readings, sorted only once."""
    points = sorted((entry["at"], dec(entry["sgv"])) for entry in entries if entry.get("sgv") is not None)
    times = [at for at, _ in points]

    def at_moment(moment: datetime) -> dict:
        index = bisect_left(times, moment)
        if index < len(times) and times[index] == moment:
            return {"value": points[index][1], "before": moment, "after": moment}
        if index == 0 or index == len(times):
            raise ValueError(tr("CGM-Werte um den Messzeitpunkt fehlen"))
        before, after = points[index - 1], points[index]
        gap = minutes_between(after[0], before[0])
        if gap > max_gap_minutes:
            raise ValueError(tr("CGM-Lücke am Messzeitpunkt ist zu groß"))
        fraction = minutes_between(moment, before[0]) / gap
        value = before[1] + (after[1] - before[1]) * fraction
        return {"value": value, "before": before[0], "after": after[0]}
    return at_moment


def finger_glucose(treatments: list[dict], moment: datetime, max_gap_minutes: int = 15) -> dict | None:
    """The blood glucose measured by hand (Nightscout BG Check) nearest to moment, within the gap."""
    return core.call(
        "calculation.finger_glucose",
        treatments=treatments, moment=moment, max_gap_minutes=max_gap_minutes)


# Injecting first and eating a little later is common (user report 2026-11-15: 10 IE at 06:43, breakfast
# entered at 07:05 without insulin gave no factor until the window was moved by hand). A rapid dose without
# carbs at most this long before a main meal whose own entry carries no rapid insulin is its bolus.
PREBOLUS_LEAD = timedelta(minutes=30)


def prebolus_start(episode: dict, all_treatments: list[dict], entries: list[dict], settings: dict) -> datetime | None:
    """The earliest pre-bolus of the meal, or None.

    It must be a rapid dose without carbs within PREBOLUS_LEAD before the meal, the meal's own entry
    holds no rapid insulin, no carbs lie between dose and meal, and no earlier main meal's automatic
    window still counts the dose (else it is that meal's correction)."""
    return core.call(
        "calculation.prebolus_start",
        episode=episode, all_treatments=all_treatments, entries=entries, settings=settings)


def automatic_window(episode: dict, all_treatments: list[dict], entries: list[dict], settings: dict,
                     prebolus: bool = True) -> dict:
    """Default meal window: from the meal (or its pre-bolus) for a fixed time, extended by doses,
    bounded by the next main meal."""
    return core.call(
        "calculation.automatic_window",
        episode=episode, all_treatments=all_treatments, entries=entries, settings=settings, prebolus=prebolus)


def window_totals(episode: dict, all_treatments: list[dict], start: datetime, end: datetime) -> tuple[Decimal, Decimal]:
    """Carbs and rapid insulin a window counts, as calculate_episode sums them: half-open
    [start, end), every dose in full, and the meal's own entry always."""
    return core.call(
        "calculation.window_totals",
        episode=episode, all_treatments=all_treatments, start=start, end=end)


def calculate_episode(episode: dict, all_treatments: list[dict], entries: list[dict], settings: dict,
                      sport_effect_mgdl: Decimal = Decimal(0), manual_window: dict | None = None) -> dict:
    """Solve one endpoint balance for every carb and rapid dose in a meal window.

    A user-confirmed manual window replaces the automatic start and endpoint.
    Its start may lie after the recorded meal time (e.g. eating began later);
    the meal's own entry, carbs and bolus, always counts in full.
    """
    return core.call(
        "calculation.calculate_episode",
        episode=episode, all_treatments=all_treatments, entries=entries, settings=settings, sport_effect_mgdl=sport_effect_mgdl, manual_window=manual_window)
