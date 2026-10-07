"""The user's own decisions on meals and days (enrichment of the Nightscout data)."""
from __future__ import annotations

from datetime import date

from app import calculation, storage
from app.common import parse_instant
from app.services import day_view, settings as settings_service


def set_sport_mark(payload: dict) -> dict:
    meal_id = payload.get("meal_id")
    status = payload.get("status")
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    if not isinstance(meal_id, str) or not 0 < len(meal_id) <= 200 or status not in ("auto", "affected", "unaffected"):
        raise ValueError("Ungültige Sport-Markierung")
    if meal_id not in {meal["id"] for meal in day_view.day_payload(day, refresh=False)["meals"]}:
        raise ValueError("Mahlzeit nicht gefunden")
    effect = calculation.dec(payload.get("effect_mgdl", 0))
    if not effect.is_finite() or not -500 <= effect <= 500:
        raise ValueError("Ungültiger Sporteffekt")
    storage.set_analysis_sport_mark(meal_id, status, str(effect))
    return {"meal_id": meal_id, "status": status, "effect_mgdl": str(effect)}


def set_window(payload: dict) -> dict:
    meal_id = payload.get("meal_id")
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    if not isinstance(meal_id, str) or not 0 < len(meal_id) <= 200:
        raise ValueError("Ungültige Mahlzeit")
    meal = next((row for row in day_view.day_payload(day, refresh=False)["meals"] if row["id"] == meal_id), None)
    if meal is None:
        raise ValueError("Mahlzeit nicht gefunden")
    if payload.get("reset"):
        storage.set_analysis_window(meal_id, None, None)
        return {"meal_id": meal_id, "manual": False}
    meal_at = parse_instant(meal["at"])
    start = parse_instant(str(payload["start"]))
    end = parse_instant(str(payload["end"]))
    settings_service.validate_manual_window(meal_at, start, end)
    storage.set_analysis_window(meal_id, start, end)
    return {"meal_id": meal_id, "manual": True,
            "start": start.isoformat(), "end": end.isoformat()}


def set_day_type(payload: dict) -> dict:
    day = payload.get("day")
    day_type = payload.get("day_type")
    if not isinstance(day, str) or not isinstance(day_type, str) or day_type not in ("work", "free"):
        raise ValueError("Ungültige Tageskennzeichnung")
    date.fromisoformat(day)
    storage.set_analysis_day_type(day, day_type)
    return {"day": day, "day_type": day_type}


def set_exclusion(payload: dict) -> dict:
    meal_id = payload.get("meal_id")
    excluded = payload.get("excluded")
    if not isinstance(meal_id, str) or not 0 < len(meal_id) <= 200 or not isinstance(excluded, bool):
        raise ValueError("Ungültige Analyse-Auswahl")
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    if meal_id not in {meal["id"] for meal in day_view.day_payload(day, refresh=False)["meals"]}:
        raise ValueError("Mahlzeit nicht gefunden")
    storage.set_analysis_exclusion(meal_id, excluded)
    return {"meal_id": meal_id, "excluded": excluded}
