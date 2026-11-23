"""The user's own decisions on meals and days (enrichment of the Nightscout data)."""
from __future__ import annotations

from datetime import date

from app import calculation
from app.common import parse_instant
from app.services import day_view, settings as settings_service
from app.store import marks


def set_sport_session(payload: dict) -> dict:
    """Philipp's glucose drop for one sport session (reset: back to the setting of its kind)."""
    session_id = payload.get("id")
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    if not isinstance(session_id, str) or not 0 < len(session_id) <= 200:
        raise ValueError("Ungültige Sporteinheit")
    if session_id not in {item["id"] for item in day_view.day_payload(day, refresh=False)["timeline"]
                          if item["kind"] == "exercise"}:
        raise ValueError("Sporteinheit nicht gefunden")
    # The effect duration is optional: "minutes" set (a number) or reset (null) it, absent leaves it.
    if "minutes" in payload:
        minutes = payload["minutes"]
        if minutes is not None:
            try:
                minutes = int(minutes)
            except (TypeError, ValueError):
                raise ValueError("Ungültige Wirkdauer") from None
            if not 30 <= minutes <= 1440:
                raise ValueError("Sport-Wirkdauer muss zwischen 30 und 1440 Minuten liegen")
        marks.set_sport_session_minutes(session_id, minutes)
    if payload.get("reset"):
        marks.set_sport_session_effect(session_id, None)
        return {"id": session_id, "effect_mgdl": None}
    if "effect_mgdl" not in payload and "minutes" in payload:
        return {"id": session_id, "minutes": payload["minutes"]}
    effect = calculation.dec(payload.get("effect_mgdl", 0))
    if not effect.is_finite() or not -500 <= effect <= 500:
        raise ValueError("Ungültiger Sporteffekt")
    marks.set_sport_session_effect(session_id, str(effect))
    return {"id": session_id, "effect_mgdl": str(effect)}


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
        marks.set_analysis_window(meal_id, None, None)
        _check_window(day, meal_id)
        return {"meal_id": meal_id, "manual": False}
    meal_at = parse_instant(meal["at"])
    start = parse_instant(str(payload["start"]))
    end = parse_instant(str(payload["end"]))
    settings_service.validate_manual_window(meal_at, start, end)
    marks.set_analysis_window(meal_id, start, end)
    _check_window(day, meal_id)
    return {"meal_id": meal_id, "manual": True,
            "start": start.isoformat(), "end": end.isoformat()}


def _check_window(day: str, meal_id: str) -> None:
    """Saving a window in the editor means Philipp looked at it: the meal counts as checked for the
    window it now has (recalculated here, since a reset gives back the automatic window)."""
    meal = next((row for row in day_view.day_payload(day, refresh=False)["meals"] if row["id"] == meal_id), None)
    if meal and meal.get("window"):
        marks.set_window_check(meal_id, str(meal["window"]["start"]), str(meal["window"]["end"]))


def set_window_check(payload: dict) -> dict:
    """"Passt" in Verlauf (user request 2026-11-23): the meal's current window is right. Holds until the
    window changes; `checked: false` takes it back. Only a finished meal can be checked."""
    meal_id = payload.get("meal_id")
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    checked = payload.get("checked")
    if not isinstance(meal_id, str) or not 0 < len(meal_id) <= 200 or not isinstance(checked, bool):
        raise ValueError("Ungültige Prüfung")
    current = day_view.with_checks(day_view.day_payload(day, refresh=False))
    meal = next((row for row in current["meals"] if row["id"] == meal_id), None)
    if meal is None:
        raise ValueError("Mahlzeit nicht gefunden")
    if checked and not meal["review"]["checkable"]:
        raise ValueError("Die Mahlzeit läuft noch; prüfen geht nach dem Ende des Zeitraums.")
    if checked:
        marks.set_window_check(meal_id, str(meal["window"]["start"]), str(meal["window"]["end"]))
    else:
        marks.set_window_check(meal_id, None, None)
    return day_view.with_checks(current)


def set_day_type(payload: dict) -> dict:
    day = payload.get("day")
    day_type = payload.get("day_type")
    if not isinstance(day, str) or not isinstance(day_type, str) or day_type not in ("work", "free"):
        raise ValueError("Ungültige Tageskennzeichnung")
    date.fromisoformat(day)
    marks.set_analysis_day_type(day, day_type)
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
    marks.set_analysis_exclusion(meal_id, excluded)
    return {"meal_id": meal_id, "excluded": excluded}
