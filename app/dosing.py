"""Transparent carbohydrate and current-glucose arithmetic for the diary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP

from app import calculation, glucose
from app.on_board import FORECAST_SPREAD_MGDL

# Trends that rule out the lower target: glucose already falls without what still acts.
FALLING = ("down", "down_fast")
# The lower target only by day and while less than this still acts (insulin + carbs + surcharge): in the
# data of 11.09.–14.10. all lows followed corrections from 19:50, and the extra lows at target 100 all had
# ≥ 96 mg/dL still acting (analysis in the project files analysen/tiefer-zielwert.md, 2026-10-14).
LOW_TARGET_HOURS = (6, 20)
LOW_TARGET_MAX_ACTING_MGDL = Decimal(150)


def half_units(units: Decimal) -> Decimal:
    """Round to the next injectable 0.5 IE step."""
    return (units * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2


def suggest(carbs: object, settings: dict, entries: list[dict],
            treatments: list[dict], now: datetime | None = None,
            activity_change: object = 0, units_per_ke: Decimal | None = None,
            auto_units_per_ke: Decimal | None = None, auto_surcharge_mgdl: Decimal | None = None,
            surcharge_share=None, on_board: dict | None = None,
            finger: list[dict] | None = None) -> dict:
    """activity_change is a signed, user-estimated BG change in mg/dL from sport
    (negative lowers BG and therefore the dose; positive, e.g. adrenaline, raises it).
    units_per_ke replaces Nightscout's current I:C, e.g. with the breakfast factor of the day type.
    auto_units_per_ke is the cautious percentile of recent calculated meal factors (Automatik).
    auto_surcharge_mgdl, when given, adds the fixed main-meal surcharge to the auto dose:
    surcharge_mgdl × surcharge_share(grams) / KF IE (surcharge method; never for snacks).
    For each factor the result holds four variants: correction to the normal target, correction to
    the cautious target, correction to the lower target (dose_low_target_mgdl, from the same forecast as
    normal) and carbs only (plus sport, no BG correction; needs no fresh CGM value). low_blocked says why
    the lower target is ruled out: below 70 mg/dL, falling, evening or night (LOW_TARGET_HOURS), or
    LOW_TARGET_MAX_ACTING_MGDL or more still acting.
    on_board (app.on_board.summary) moves the start glucose by what still acts: normal and carbs only use
    the full forecast (− remaining insulin + remaining carbs and surcharge); cautious uses that forecast
    minus its typical miss (FORECAST_SPREAD_MGDL), but never less than letting the remaining
    carbs and surcharge only cancel active insulin. Below 70 mg/dL both only cancel active insulin.
    finger holds blood glucose measured by hand ({"at", "mgdl"}); a measurement newer than the latest
    CGM value is the start glucose instead (same 15-minute freshness, no trend)."""
    now = now or datetime.now(timezone.utc)
    grams = calculation.dec(carbs)
    if not grams.is_finite() or not 0 <= grams <= 500:
        raise ValueError("Kohlenhydrate müssen zwischen 0 und 500 g liegen")
    activity = calculation.dec(activity_change if activity_change not in (None, "") else 0)
    if not activity.is_finite() or not -500 <= activity <= 500:
        raise ValueError("Sporteinfluss muss zwischen -500 und 500 mg/dL liegen")
    timezone_name = settings["timezone"]
    points = sorted((entry["at"], calculation.dec(entry["sgv"])) for entry in entries
                    if entry.get("sgv") is not None and entry["at"] <= now)
    latest = points[-1] if points else None
    checks = sorted((item["at"], calculation.dec(item["mgdl"])) for item in finger or [] if item["at"] <= now)
    by_hand = bool(checks and (latest is None or checks[-1][0] > latest[0]))
    if by_hand:
        latest = checks[-1]
    glucose_at = latest[0] if latest else None
    fresh = bool(latest and now - latest[0] <= timedelta(minutes=15))
    recent = [
        {"at": item["at"].isoformat(), "units": str(item["insulin"])}
        for item in treatments
        if now - timedelta(hours=3) <= item["at"] <= now
        and calculation.dec(item.get("insulin") or 0) > 0
        and calculation.is_rapid_insulin(item)
    ]
    result = {
        "carbs_g": str(grams),
        "glucose_mgdl": str(latest[1]) if latest else None,
        "glucose_at": glucose_at.isoformat() if glucose_at else None,
        "glucose_fresh": fresh,
        "glucose_source": ("finger" if by_hand else "cgm") if latest else None,
        **((None if by_hand else glucose.trend(points)) or glucose.NO_TREND),
        "recent_rapid_doses": recent,
        "activity_change_mgdl": str(activity),
    }
    try:
        ratio = calculation.schedule_value_at(
            now, settings.get("ic_schedule", []), timezone_name, "grams_per_unit")
        isf = calculation.isf_at(now, settings.get("isf_schedule", []), timezone_name)
    except ValueError:
        result["reason"] = "I:C- oder Korrekturfaktor-Profil aus Nightscout fehlt"
        return result
    result["nightscout_units_per_ke"] = str(Decimal(10) / ratio)
    carb_units = grams / ratio if units_per_ke is None else grams * units_per_ke / 10
    result.update({
        "units_per_ke": str(Decimal(10) / ratio if units_per_ke is None else units_per_ke), "carb_units": str(carb_units),
        "isf_mgdl_per_unit": str(isf),
        "activity_units": str(activity / isf),
    })
    target = calculation.dec(settings.get("dose_target_mgdl", 100))
    cautious_target = calculation.dec(settings.get("dose_cautious_target_mgdl", 140))
    low_target = calculation.dec(settings.get("dose_low_target_mgdl", 100))
    result.update({
        "target_mgdl": str(target),
        "cautious_target_mgdl": str(cautious_target),
        "low_target_mgdl": str(low_target),
    })
    auto_carb_units = grams * auto_units_per_ke / 10 if auto_units_per_ke is not None else None
    if auto_carb_units is not None:
        result["auto_units_per_ke"] = str(auto_units_per_ke)
        if auto_surcharge_mgdl is not None and grams > 0:
            surcharge_units = auto_surcharge_mgdl * surcharge_share(grams) / isf
            result["auto_surcharge_units"] = str(surcharge_units)
            auto_carb_units += surcharge_units
        result["auto_carb_units"] = str(auto_carb_units)
    lowering = on_board["lowering_mgdl"] if on_board else Decimal(0)
    raising = on_board["raising_mgdl"] if on_board else Decimal(0)
    if fresh:
        guarded = latest[1] + activity - max(Decimal(0), lowering - raising)
        full = latest[1] + activity - lowering + raising
        # Cautious starts from the lower edge of the forecast's typical miss; below 70 mg/dL carbs
        # still to come never justify insulin now, for either suggestion.
        low = latest[1] < glucose.LOW_MGDL
        cautious_start = guarded if low else max(guarded, full - FORECAST_SPREAD_MGDL)
        start = guarded if low else full
        hour = now.astimezone(ZoneInfo(timezone_name)).hour
        if low_target >= target:
            result["low_blocked"] = "off"
        elif low:
            result["low_blocked"] = "low"
        elif result.get("trend_direction") in FALLING:
            result["low_blocked"] = "falling"
        elif not LOW_TARGET_HOURS[0] <= hour < LOW_TARGET_HOURS[1]:
            result["low_blocked"] = "evening"
        elif lowering + raising >= LOW_TARGET_MAX_ACTING_MGDL:
            result["low_blocked"] = "acting"
    for prefix, units in (("", carb_units), ("auto_", auto_carb_units)):
        if units is not None:
            carbs_total = max(Decimal(0), units + activity / isf)
            result[prefix + "carbs_total_units"] = str(carbs_total)
            result[prefix + "carbs_entry_units"] = str(half_units(carbs_total))
            if fresh:
                result[prefix + "carbs_expected_glucose_mgdl"] = str(
                    start + (units - half_units(carbs_total)) * isf)
    if fresh:
        # Sport shifts the modelled start glucose, so its effect flows through the correction factor;
        # so do the insulin, carbs and surcharge still acting.
        result["forecast_mgdl"] = str(start)
        result["cautious_forecast_mgdl"] = str(cautious_start)
        correction_units = (start - target) / isf
        cautious_correction_units = (cautious_start - cautious_target) / isf
        total = max(Decimal(0), carb_units + correction_units)
        cautious_total = max(Decimal(0), carb_units + cautious_correction_units)
        entry_units = (total * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2
        cautious_entry_units = (cautious_total * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2
        result["correction_units"] = str(correction_units)
        result["total_units"] = str(total)
        result["entry_units"] = str(entry_units)
        result["expected_glucose_mgdl"] = str(start + (carb_units - entry_units) * isf)
        result["cautious_correction_units"] = str(cautious_correction_units)
        result["cautious_total_units"] = str(cautious_total)
        result["cautious_entry_units"] = str(cautious_entry_units)
        result["cautious_expected_glucose_mgdl"] = str(cautious_start + (carb_units - cautious_entry_units) * isf)
        low_correction_units = (start - low_target) / isf
        low_total = max(Decimal(0), carb_units + low_correction_units)
        result["low_correction_units"] = str(low_correction_units)
        result["low_total_units"] = str(low_total)
        result["low_entry_units"] = str(half_units(low_total))
        result["low_expected_glucose_mgdl"] = str(start + (carb_units - half_units(low_total)) * isf)
        if auto_carb_units is not None:
            auto_low_total = max(Decimal(0), auto_carb_units + low_correction_units)
            result["auto_low_total_units"] = str(auto_low_total)
            result["auto_low_entry_units"] = str(half_units(auto_low_total))
            result["auto_low_expected_glucose_mgdl"] = str(
                start + (auto_carb_units - half_units(auto_low_total)) * isf)
            auto_total = max(Decimal(0), auto_carb_units + correction_units)
            auto_entry_units = (auto_total * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2
            result["auto_total_units"] = str(auto_total)
            result["auto_entry_units"] = str(auto_entry_units)
            # Expected glucose follows the auto factor's own carb model, not Nightscout's I:C.
            result["auto_expected_glucose_mgdl"] = str(start + (auto_carb_units - auto_entry_units) * isf)
            auto_cautious_total = max(Decimal(0), auto_carb_units + cautious_correction_units)
            auto_cautious_entry_units = half_units(auto_cautious_total)
            result["auto_cautious_total_units"] = str(auto_cautious_total)
            result["auto_cautious_entry_units"] = str(auto_cautious_entry_units)
            result["auto_cautious_expected_glucose_mgdl"] = str(
                cautious_start + (auto_carb_units - auto_cautious_entry_units) * isf)
    else:
        result["reason"] = ("Kein aktueller CGM-Wert innerhalb von 15 Minuten. "
                            "Ein blutig gemessener Wert kann ihn ersetzen.")
    return result
