"""Transparent carbohydrate and current-glucose arithmetic for the diary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from app import calculation


def suggest(carbs: object, settings: dict, entries: list[dict],
            treatments: list[dict], now: datetime | None = None,
            activity_change: object = 0, units_per_ke: Decimal | None = None,
            auto_units_per_ke: Decimal | None = None) -> dict:
    """activity_change is a signed, user-estimated BG change in mg/dL from sport
    (negative lowers BG and therefore the dose; positive, e.g. adrenaline, raises it).
    units_per_ke replaces Nightscout's current I:C, e.g. with the breakfast factor of the day type.
    auto_units_per_ke is the cautious percentile of recent calculated meal factors; it yields a third
    suggestion that corrects to the normal target."""
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
    glucose_at = latest[0] if latest else None
    fresh = bool(latest and now - latest[0] <= timedelta(minutes=15))
    prior = next(((at, value) for at, value in reversed(points[:-1])
                  if timedelta(minutes=5) <= latest[0] - at <= timedelta(minutes=20)), None) if latest else None
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
        "trend_delta_mgdl": str(latest[1] - prior[1]) if prior else None,
        "trend_minutes": str(calculation.minutes_between(latest[0], prior[0])) if prior else None,
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
    result.update({
        "target_mgdl": str(target),
        "cautious_target_mgdl": str(cautious_target),
    })
    auto_carb_units = grams * auto_units_per_ke / 10 if auto_units_per_ke is not None else None
    if auto_carb_units is not None:
        result["auto_units_per_ke"] = str(auto_units_per_ke)
        result["auto_carb_units"] = str(auto_carb_units)
    if fresh:
        # Sport shifts the modelled start glucose, so its effect flows through the correction factor.
        start = latest[1] + activity
        correction_units = (start - target) / isf
        cautious_correction_units = (start - cautious_target) / isf
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
        result["cautious_expected_glucose_mgdl"] = str(start + (carb_units - cautious_entry_units) * isf)
        if auto_carb_units is not None:
            auto_total = max(Decimal(0), auto_carb_units + correction_units)
            auto_entry_units = (auto_total * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2
            result["auto_total_units"] = str(auto_total)
            result["auto_entry_units"] = str(auto_entry_units)
            # Expected glucose follows the auto factor's own carb model, not Nightscout's I:C.
            result["auto_expected_glucose_mgdl"] = str(start + (auto_carb_units - auto_entry_units) * isf)
    else:
        result["reason"] = "Kein aktueller CGM-Wert innerhalb von 15 Minuten"
    return result
