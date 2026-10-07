"""Night overview from the last rapid dose to breakfast."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app import calculation, episodes



def night_analysis(day: str, treatments: list[dict], entries: list[dict], meals: list[dict], settings: dict) -> dict:
    """Summarize CGM values from three hours after the last rapid dose to breakfast."""
    evening_meals = [item for item in meals if item.get("meal") == "dinner"]
    if not evening_meals:
        return {"reason": "Für diesen Tag ist kein Abendessen erfasst."}
    dinner = max(evening_meals, key=lambda item: item["at"])

    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    next_meals = episodes.meals_for_day(treatments, next_day, settings["timezone"], settings)
    breakfasts = [item for item in next_meals
                  if item.get("meal") == "breakfast" and item["at"] > dinner["at"]]
    if not breakfasts:
        return {"reason": "Für den folgenden Tag ist kein Frühstück erfasst."}
    breakfast = min(breakfasts, key=lambda item: item["at"])

    rapid_doses = [item["at"] for item in treatments
                   if dinner["at"] <= item["at"] < breakfast["at"]
                   and Decimal(str(item.get("insulin") or 0)) > 0
                   and calculation.is_rapid_insulin(item)]
    if not rapid_doses:
        return {
            "reason": "Zwischen Abendessen und Frühstück ist keine schnell wirksame Injektion erfasst.",
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
        }
    last_injection_at = max(rapid_doses)
    start = last_injection_at + timedelta(hours=3)
    end = breakfast["at"]
    if end <= start:
        return {
            "reason": "Das Frühstück beginnt vor dem Nachtstart drei Stunden nach der letzten Injektion.",
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
            "last_injection_at": last_injection_at, "start": start, "end": end,
        }
    try:
        start_glucose = calculation.interpolate_glucose(entries, start)["value"]
        end_glucose = calculation.interpolate_glucose(entries, end)["value"]
    except ValueError as error:
        return {
            "reason": str(error), "start": start, "end": end,
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
            "last_injection_at": last_injection_at,
        }

    candidates = [(start_glucose, start), (end_glucose, end)]
    candidates.extend(
        (calculation.dec(item["sgv"]), item["at"])
        for item in entries if start < item["at"] < end and item.get("sgv") is not None
    )
    minimum, minimum_at = min(candidates, key=lambda point: point[0])
    return {
        "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
        "last_injection_at": last_injection_at,
        "start": start, "end": end,
        "start_glucose": start_glucose, "end_glucose": end_glucose,
        "delta_mgdl": end_glucose - start_glucose,
        "minimum_glucose": minimum, "minimum_at": minimum_at,
    }
