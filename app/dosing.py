"""Transparent carbohydrate and current-glucose arithmetic for the diary."""
from __future__ import annotations

from app import core
from datetime import datetime
from decimal import Decimal


def half_units(units: Decimal) -> Decimal:
    """Round to the next injectable 0.5 IE step."""
    return core.call("dosing.half_units", units=units)


def suggest(carbs: object, settings: dict, entries: list[dict],
            treatments: list[dict], now: datetime | None = None,
            activity_change: object = 0, units_per_ke: Decimal | None = None,
            auto_units_per_ke: Decimal | None = None, auto_surcharge_mgdl: Decimal | None = None,
            surcharge_share=None, on_board: dict | None = None,
            finger: list[dict] | None = None, hold_rise: bool = False) -> dict:
    """activity_change is a signed, user-estimated BG change in mg/dL from sport
    (negative lowers BG and therefore the dose; positive, e.g. adrenaline, raises it).
    units_per_ke replaces Nightscout's current I:C, e.g. with the breakfast factor of the day type.
    auto_units_per_ke is the cautious percentile of recent calculated meal factors (Automatik).
    auto_surcharge_mgdl, when given, adds the fixed main-meal surcharge to the auto dose:
    surcharge_mgdl × surcharge_share(grams) / KF IE (surcharge method; surcharge_share gives the share these
    carbs add to their meal, app/services/dose_help.py).
    For each factor the result holds three variants: correction to the normal target, correction to
    the cautious target and carbs only (plus sport, no BG correction; needs no fresh CGM value).
    on_board (app.on_board.summary) moves the start glucose by what still acts: normal and carbs only use
    the full forecast (− remaining insulin + remaining carbs and surcharge); cautious uses that forecast
    minus its typical miss (FORECAST_SPREAD_MGDL), but never less than letting the remaining
    carbs and surcharge only cancel active insulin. Below 70 mg/dL the same forecast applies (no special rule,
    user request 2026-10-23).
    finger holds blood glucose measured by hand ({"at", "mgdl"}); a measurement newer than the latest
    CGM value is the start glucose instead (same 15-minute freshness, no trend).
    hold_rise: the forecast's rise above
    a target belongs to that second part, which follows later from the forecast (app/split.py), so the options
    only correct downwards; their expected values then assume that second part."""
    surcharge_share = core.share_value(surcharge_share, carbs)
    now = core.now_or(now)
    return core.call(
        "dosing.suggest",
        carbs=carbs, settings=settings, entries=entries, treatments=treatments, now=now, activity_change=activity_change, units_per_ke=units_per_ke, auto_units_per_ke=auto_units_per_ke, auto_surcharge_mgdl=auto_surcharge_mgdl, surcharge_share=surcharge_share, on_board=on_board, finger=finger, hold_rise=hold_rise)
