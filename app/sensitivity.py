"""Compare current correction factors against recorded meal deviations."""
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


def weighted_median(values: list[tuple[Decimal, Decimal]]) -> Decimal:
    """Minimize the sum of weighted absolute deviations."""
    ordered = sorted(values)
    halfway = sum((weight for _, weight in ordered), Decimal(0)) / 2
    weight_so_far = Decimal(0)
    for value, weight in ordered:
        weight_so_far += weight
        if weight_so_far >= halfway:
            return value
    return ordered[-1][0]


def compare_schedule(meals: list[dict], isf_schedule: list[dict], ic_schedule: list[dict],
                     timezone: str) -> list[dict]:
    """Minimize existing absolute meal deviations for each schedule slot."""
    if not isf_schedule or not ic_schedule:
        return []
    rows = []
    for meal in meals:
        result = meal.get("calculation") or {}
        if (meal.get("excluded") or meal.get("deviation_mgdl") is None
                or result.get("carb_events") is None
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
        rows.append({"exposure": exposure, "error": dec(meal["deviation_mgdl"])})
    output = []
    for slot in sorted(isf_schedule, key=lambda row: int(row["start_minute"])):
        minute = int(slot["start_minute"])
        current = dec(slot["mgdl_per_unit"])
        relevant = [row for row in rows if row["exposure"][minute] != 0]
        item = {"start_minute": minute, "current_mgdl_per_unit": str(current),
                "meal_count": len(relevant),
                "candidate_mgdl_per_unit": None, "baseline_mae_mgdl": None,
                "candidate_mae_mgdl": None, "reason": None}
        if not relevant:
            item["reason"] = "Keine Mahlzeit mit Einfluss auf diesen Zeitabschnitt"
            output.append(item)
            continue
        candidate = weighted_median([
            (current + row["error"] / row["exposure"][minute], abs(row["exposure"][minute]))
            for row in relevant
        ])
        if not Decimal("0.001") <= candidate <= Decimal(1000):
            item["reason"] = "Kein gültiger positiver Vergleichswert"
            output.append(item)
            continue
        item["candidate_mgdl_per_unit"] = str(candidate)
        item["baseline_mae_mgdl"] = str(mean_absolute([row["error"] for row in relevant]))
        item["candidate_mae_mgdl"] = str(mean_absolute([
            row["error"] - (candidate - current) * row["exposure"][minute]
            for row in relevant
        ]))
        output.append(item)
    return output
