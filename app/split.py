"""Split bolus for large main meals: a share of the dose now, the rest later.

Rapid insulin is mostly done after 2 h while a large meal and its fat/protein still raise glucose for hours,
so the full dose up front means too much insulin early and too little late. The plan is stored with
the meal entry in Nightscout ([DASP:<IE>@<minutes>]); any later rapid dose without carbs counts as its second
part, which the dose help suggests from the current forecast (app/on_board.py) next to the planned amount.
Carbs eaten while it is open (a dessert) get a dose for themselves only: the forecast's rise stays with the second
part.

When the second part fits best follows the glucose course: 60 to 105 min after the meal, glucose from
180 mg/dL rising by 15 mg/dL per 15 min or more means the carbs act faster or were more, so it is due
early; at the planned time, glucose below 110 or falling by more than 5 mg/dL per 15 min means the first
part still lowers enough, so it waits (checked again every round) until 60 min past the planned time.
Its amount is always the current forecast's correction; the planned IE stay as orientation.
"""
from __future__ import annotations

from app import core
from datetime import datetime, timedelta
from decimal import Decimal


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


def offer(carbs: Decimal, snack: bool, settings: dict, fat: bool = False) -> dict | None:
    """Whether the entry form offers (and preselects) a split for this meal: a main meal from split_min_grams,
    or any fat/protein main meal, whose fat surcharge would otherwise be injected at once while it acts
    over hours."""
    return core.call("split.offer", carbs=carbs, snack=snack, settings=settings, fat=fat)


def worth_it(full_units, settings: dict) -> bool:
    """Whether the meal's full dose is large enough to split (user choice 2026-11-06: not under 5 IE;
    raised to 6.5 IE 2026-11-17).
    A small second part hardly acts and costs an extra injection; a low glucose shrinking the dose
    below the limit means the whole dose is given at once."""
    return core.call("split.worth_it", full_units=full_units, settings=settings)


def parts(full_units: Decimal, first_percent: int, later_units: Decimal | None = None) -> tuple[Decimal, Decimal]:
    """The injectable first part (0.5 IE steps) and the rest of an already rounded dose. With later_units
    (the calculated fat/protein surcharge, app/fpe.py) the rest is that amount, at most the whole dose."""
    return core.call("split.parts", full_units=full_units, first_percent=first_percent, later_units=later_units)


def open_plan(treatments: list[dict], now: datetime) -> dict | None:
    """The latest planned second part that has not been injected yet."""
    return core.call("split.open_plan", treatments=treatments, now=now)


def timing(plan: dict, entries: list[dict], settings: dict, now: datetime) -> dict:
    """When the second part fits now: early, due, wait (glucose low or falling at the planned time) or later.
    Without a fresh CGM value the planned time decides."""
    return core.call("split.timing", plan=plan, entries=entries, settings=settings, now=now)
