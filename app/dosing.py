"""Transparent carbohydrate and current-glucose arithmetic for the diary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from app import calculation


def suggest(carbs: object, settings: dict, entries: list[dict],
            treatments: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    grams = calculation.dec(carbs)
    if not grams.is_finite() or not 0 <= grams <= 500:
        raise ValueError("Kohlenhydrate müssen zwischen 0 und 500 g liegen")
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
    }
    try:
        ratio = calculation.schedule_value_at(
            now, settings.get("ic_schedule", []), timezone_name, "grams_per_unit")
        isf = calculation.isf_at(now, settings.get("isf_schedule", []), timezone_name)
    except ValueError:
        result["reason"] = "I:C- oder Korrekturfaktor-Profil aus Nightscout fehlt"
        return result
    carb_units = grams / ratio
    result.update({
        "units_per_ke": str(Decimal(10) / ratio), "carb_units": str(carb_units),
        "isf_mgdl_per_unit": str(isf),
    })
    target = calculation.dec(settings.get("dose_target_mgdl", 100))
    cautious_target = Decimal(140)
    result.update({
        "target_mgdl": str(target),
        "cautious_target_mgdl": str(cautious_target),
    })
    if fresh:
        correction_units = (latest[1] - target) / isf
        cautious_correction_units = (latest[1] - cautious_target) / isf
        total = max(Decimal(0), carb_units + correction_units)
        cautious_total = max(Decimal(0), carb_units + cautious_correction_units)
        result["correction_units"] = str(correction_units)
        result["total_units"] = str(total)
        result["entry_units"] = str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        result["cautious_correction_units"] = str(cautious_correction_units)
        result["cautious_total_units"] = str(cautious_total)
        result["cautious_entry_units"] = str(cautious_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        result["reason"] = "Kein aktueller CGM-Wert innerhalb von 15 Minuten"
    return result
