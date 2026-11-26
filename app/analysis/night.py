"""Night overview from three hours after the last rapid dose to breakfast: how far glucose falls
without bolus insulin and how much it rises again towards the morning (how the basal works)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app import calculation, episodes
from app.i18n import tr

NIGHT_GAP = timedelta(hours=3)  # after a rapid dose before its effect no longer dominates the curve
MORNING = timedelta(hours=2)  # a rapid dose this close before breakfast ends the night



def night_analysis(day: str, treatments: list[dict], entries: list[dict], meals: list[dict], settings: dict) -> dict:
    """Summarize CGM values from three hours after the last rapid dose to breakfast."""
    evening_meals = [item for item in meals if item.get("meal") == "dinner"]
    if not evening_meals:
        return {"reason": tr("Für diesen Tag ist kein Abendessen erfasst.")}
    dinner = max(evening_meals, key=lambda item: item["at"])

    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    next_meals = episodes.meals_for_day(treatments, next_day, settings["timezone"], settings)
    breakfasts = [item for item in next_meals
                  if item.get("meal") == "breakfast" and item["at"] > dinner["at"]]
    if not breakfasts:
        return {"reason": tr("Für den folgenden Tag ist kein Frühstück erfasst.")}
    breakfast = min(breakfasts, key=lambda item: item["at"])

    rapid_doses = [item["at"] for item in treatments
                   if dinner["at"] <= item["at"] < breakfast["at"]
                   and Decimal(str(item.get("insulin") or 0)) > 0
                   and calculation.is_rapid_insulin(item)]
    # The night starts three hours after the evening's last rapid dose. A dose in the last two hours
    # before breakfast (a correction on waking, a pre-bolus) belongs to the morning and ends the night
    # instead, so it no longer voids the night (user request 2026-11-26: how the basal works).
    morning = [at for at in rapid_doses if at >= breakfast["at"] - MORNING]
    evening = [at for at in rapid_doses if at < breakfast["at"] - MORNING]
    if not evening:
        return {
            "reason": tr("Zwischen Abendessen und Frühstück ist keine schnell wirksame Injektion erfasst."),
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
        }
    last_injection_at = max(evening)
    morning_dose_at = min(morning) if morning else None
    end = morning_dose_at or breakfast["at"]
    start = last_injection_at + NIGHT_GAP
    if end <= start:
        return {
            "reason": tr("Frühstück oder Morgendosis kommen vor dem Nachtstart drei Stunden nach der letzten Injektion."),
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
            "last_injection_at": last_injection_at, "morning_dose_at": morning_dose_at, "start": start, "end": end,
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
    # Carbs eaten in the night (a hypo snack) lift the curve, so such a night says little about the basal.
    night_carbs = sum((calculation.dec(item.get("carbs") or 0) for item in treatments
                       if start <= item["at"] < end), Decimal(0))
    return {
        "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
        "last_injection_at": last_injection_at, "morning_dose_at": morning_dose_at,
        "start": start, "end": end,
        "start_glucose": start_glucose, "end_glucose": end_glucose,
        "delta_mgdl": end_glucose - start_glucose,
        "minimum_glucose": minimum, "minimum_at": minimum_at,
        "drop_mgdl": minimum - start_glucose, "rise_mgdl": end_glucose - minimum,
        "carbs_g": night_carbs,
    }
