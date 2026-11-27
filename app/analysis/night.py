"""Night overview from three hours after the last rapid dose to breakfast: how far glucose falls
without bolus insulin and how much it rises again towards the morning (how the basal works).

The rules live in the Rust core (core/diabetes-core/src/day.rs night_analysis, quiet_periods); the day
payload carries both, so the server only calls this for a single night."""
from __future__ import annotations

from app import core


def night_analysis(day: str, treatments: list[dict], entries: list[dict], meals: list[dict], settings: dict) -> dict:
    """Summarize CGM values from three hours after the last rapid dose to breakfast."""
    return core.call("night.night_analysis", day=day, treatments=treatments, entries=entries,
                     meals=[{"meal": meal.get("meal"), "at": meal.get("at")} for meal in meals], settings=settings)
