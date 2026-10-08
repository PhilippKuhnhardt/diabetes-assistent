"""Split bolus for large main meals (user request 2026-10-08): a share of the dose now, the rest later.

Fiasp is mostly done after 2 h while a large meal and its fat/protein still raise glucose for hours,
so the full dose up front means too much insulin early and too little late. The plan is stored with
the meal entry in Nightscout ([DASP:<IE>@<minutes>]); any later rapid dose counts as its second part,
which the dose help suggests from the current forecast (app/on_board.py) next to the planned amount.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app import calculation, dosing, on_board

# A plan stays open this long past its due time, then it is no longer offered.
OPEN_AFTER_DUE = timedelta(hours=3)
# A dose this soon after the meal still belongs to its first part.
FIRST_PART_MINUTES = timedelta(minutes=15)


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
