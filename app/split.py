"""Split bolus for large main meals (user request 2026-10-08): a share of the dose now, the rest later.

Fiasp is mostly done after 2 h while a large meal and its fat/protein still raise glucose for hours,
so the full dose up front means too much insulin early and too little late. The plan is stored with
the meal entry in Nightscout ([DASP:<IE>@<minutes>]); any later rapid dose counts as its second part,
which the dose help suggests from the current forecast (app/on_board.py) next to the planned amount.

When the second part fits best follows the glucose course (user approval 2026-10-22, analysis in the
project files analysen/pruefstand-2026-10/split-bolus.md): 60 to 105 min after the meal, glucose from
180 mg/dL rising by 15 mg/dL per 15 min or more means the carbs act faster or were more, so it is due
early; at the planned time, glucose below 110 or falling by more than 5 mg/dL per 15 min means the first
part still lowers enough, so it waits (checked again every round) until 60 min past the planned time.
Its amount is always the current forecast's correction; the planned IE stay as orientation.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app import calculation, dosing, glucose, on_board

# A plan stays open this long past its due time, then it is no longer offered.
OPEN_AFTER_DUE = timedelta(hours=3)
# A dose this soon after the meal still belongs to its first part.
FIRST_PART_MINUTES = timedelta(minutes=15)
# Early second part: from this long after the meal until EARLY_BEFORE_DUE before the planned time ...
EARLY_FROM = timedelta(minutes=60)
EARLY_BEFORE_DUE = timedelta(minutes=15)
# ... with glucose at least this high and rising at least this much per 15 minutes.
EARLY_MGDL, EARLY_RISE_MGDL = Decimal(180), Decimal(15)
# Waiting at the planned time: glucose below this or falling by more than this per 15 minutes ...
WAIT_BELOW_MGDL, WAIT_FALL_MGDL = Decimal(110), Decimal(5)
# ... for at most this long past the planned time.
WAIT_AT_MOST = timedelta(minutes=60)


def offer(carbs: Decimal, snack: bool, settings: dict) -> dict | None:
    """Whether the entry form offers (and preselects) a split for this meal."""
    minimum = int(settings.get("split_min_grams", 80))
    if snack or minimum <= 0 or carbs < minimum:
        return None
    return {"first_percent": int(settings.get("split_first_percent", 60)),
            "delay_minutes": int(settings.get("split_delay_minutes", 120))}


def parts(full_units: Decimal, first_percent: int) -> tuple[Decimal, Decimal]:
    """The injectable first part (0.5 IE steps) and the rest of an already rounded dose."""
    now = dosing.half_units(full_units * Decimal(first_percent) / 100)
    return now, full_units - now


def open_plan(treatments: list[dict], now: datetime) -> dict | None:
    """The latest planned second part that has not been injected yet."""
    meals = [item for item in treatments if item.get("split") and on_board.is_main_meal(item) and item["at"] <= now]
    if not meals:
        return None
    meal = max(meals, key=lambda item: item["at"])
    due = meal["at"] + timedelta(minutes=int(meal["split"]["delay_minutes"]))
    if now > due + OPEN_AFTER_DUE:
        return None
    later_doses = [item for item in treatments
                   if meal["at"] + FIRST_PART_MINUTES < item["at"] <= now and calculation.is_rapid_insulin(item)
                   and calculation.dec(item.get("insulin") or 0) > 0]
    if later_doses:
        return None
    return {"meal_at": meal["at"], "units": calculation.dec(meal["split"]["units"]), "due_at": due}


def timing(plan: dict, entries: list[dict], settings: dict, now: datetime) -> dict:
    """When the second part fits now: early, due, wait (glucose low or falling at the planned time) or later.
    Without a fresh CGM value the planned time decides."""
    status = glucose.current_status(entries, settings, now)
    value = calculation.dec(status["glucose_mgdl"]) if status["fresh"] else None
    rise = calculation.dec(status["trend_delta_mgdl"]) if value is not None and status["trend_delta_mgdl"] else None
    latest = plan["due_at"] + WAIT_AT_MOST
    if now < plan["due_at"]:
        early = (plan["meal_at"] + EARLY_FROM <= now < plan["due_at"] - EARLY_BEFORE_DUE
                 and value is not None and rise is not None and value >= EARLY_MGDL and rise >= EARLY_RISE_MGDL)
        return {"state": "early" if early else "later", "latest_at": latest}
    waiting = (now < latest and value is not None
               and (value < WAIT_BELOW_MGDL or (rise is not None and rise < -WAIT_FALL_MGDL)))
    return {"state": "wait" if waiting else "due", "latest_at": latest}
