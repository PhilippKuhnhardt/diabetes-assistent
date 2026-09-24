"""Retrospective comparison of correction-factor schedule values.

The meal I:C schedule stays fixed. This is a descriptive model check, not a
dosing recommendation or an estimate of physiological insulin action.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.calculation import dec, schedule_value_at


def slot_at(at: datetime, schedule: list[dict], timezone: str) -> int:
    local = at.astimezone(ZoneInfo(timezone))
    minute = local.hour * 60 + local.minute
    starts = sorted(int(row["start_minute"]) for row in schedule)
    return next((start for start in reversed(starts) if start <= minute), starts[-1])


def mean_absolute(errors: list[Decimal]) -> Decimal:
    return sum((abs(error) for error in errors), Decimal(0)) / len(errors)


def compare_schedule(meals: list[dict], isf_schedule: list[dict], ic_schedule: list[dict],
                     timezone: str) -> list[dict]:
    """Fit each ISF slot on older meals; report error on newer meals only."""
    if not isf_schedule or not ic_schedule:
        return []
    rows = []
    for meal in meals:
        result = meal.get("calculation") or {}
        if (meal.get("excluded") or result.get("factor_units_per_ke") is None
                or result.get("delta_mgdl") is None or result.get("carb_events") is None
                or result.get("doses") is None):
            continue
        exposure = {int(row["start_minute"]): Decimal(0) for row in isf_schedule}
        for event in result["carb_events"]:
            at = datetime.fromisoformat(event["at"])
            slot = slot_at(at, isf_schedule, timezone)
            ratio = schedule_value_at(at, ic_schedule, timezone, "grams_per_unit")
            exposure[slot] += dec(event["carbs"]) / ratio
        for dose in result["doses"]:
            slot = slot_at(datetime.fromisoformat(dose["at"]), isf_schedule, timezone)
            exposure[slot] -= dec(dose["units"])
        expected = sum((exposure[int(row["start_minute"])] * dec(row["mgdl_per_unit"])
                        for row in isf_schedule), Decimal(0))
        rows.append({"at": meal["at"], "exposure": exposure,
                     "error": dec(result["delta_mgdl"]) - expected})
    output = []
    for slot in sorted(isf_schedule, key=lambda row: int(row["start_minute"])):
        minute = int(slot["start_minute"])
        current = dec(slot["mgdl_per_unit"])
        relevant = sorted((row for row in rows if abs(row["exposure"][minute]) >= Decimal("0.25")),
                          key=lambda row: row["at"])
        item = {"start_minute": minute, "current_mgdl_per_unit": str(current),
                "meal_count": len(relevant), "training_count": 0, "validation_count": 0,
                "candidate_mgdl_per_unit": None, "baseline_mae_mgdl": None,
                "candidate_mae_mgdl": None, "reason": None}
        if len(relevant) < 6:
            item["reason"] = "Mindestens 6 aussagekräftige Mahlzeiten nötig"
            output.append(item)
            continue
        split = max(4, len(relevant) * 2 // 3)
        training, validation = relevant[:split], relevant[split:]
        item["training_count"] = len(training)
        item["validation_count"] = len(validation)
        numerator = sum((row["exposure"][minute] * row["error"] for row in training), Decimal(0))
        denominator = sum((row["exposure"][minute] ** 2 for row in training), Decimal(0))
        candidate = current + numerator / denominator
        if not Decimal("0.001") <= candidate <= Decimal(1000):
            item["reason"] = "Kein gültiger positiver Vergleichswert"
            output.append(item)
            continue
        item["candidate_mgdl_per_unit"] = str(candidate)
        item["baseline_mae_mgdl"] = str(mean_absolute([row["error"] for row in validation]))
        item["candidate_mae_mgdl"] = str(mean_absolute([
            row["error"] - (candidate - current) * row["exposure"][minute]
            for row in validation
        ]))
        output.append(item)
    return output
