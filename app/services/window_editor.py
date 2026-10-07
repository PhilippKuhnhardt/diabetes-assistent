"""The manual window editor: CGM readings to drag over and the totals of a candidate window."""
from __future__ import annotations

from bisect import bisect_left
from datetime import date, timedelta
from decimal import Decimal

from app import calculation
from app import glucose as cgm
from app.common import parse_instant
from app.services import day_view, settings as settings_service
from app.store import config

LIMITS = settings_service.MANUAL_WINDOW_LIMITS
# The editor shows a little more than the handles can reach.
VIEW_LEAD = timedelta(minutes=LIMITS["lead_minutes"] + 30)
VIEW_LAG = timedelta(minutes=LIMITS["max_minutes"] + 60)


def _meal(payload: dict) -> tuple[dict, dict, list[dict], list[dict]]:
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    meal_id = payload.get("meal_id")
    current = day_view.day_payload(day, refresh=False)
    meal = next((row for row in current["meals"] if row["id"] == meal_id), None)
    if meal is None:
        raise ValueError("Mahlzeit nicht gefunden")
    entries = [{"at": parse_instant(row["at"]), "sgv": calculation.dec(row["sgv"])} for row in current["entries"]]
    treatments = [{**row, "at": parse_instant(row["at"])} for row in current["treatments"]]
    return meal, current, entries, treatments


def _reading(entries: list[dict], at, settings: dict) -> dict:
    try:
        value = calculation.interpolate_glucose(entries, at)["value"]
    except ValueError:
        return {"t": at.isoformat(), "value": None, "tone": "none", "trend": None}
    before = [(row["at"], row["sgv"]) for row in entries if at - timedelta(minutes=16) <= row["at"] < at]
    course = cgm.trend(before + [(at, value)])
    return {"t": at.isoformat(), "value": str(value.quantize(Decimal("0.1"))),
            "tone": cgm.tone(value, at, settings), "trend": course["trend_direction"] if course else None}


def editor(params: dict) -> dict:
    """Readings for every minute the handles can reach, plus the instants they snap to."""
    payload = {key: values[0] for key, values in params.items()}
    meal, _, entries, treatments = _meal(payload)
    _, settings = config.get_settings()
    meal_at = parse_instant(meal["at"])
    entries.sort(key=lambda row: row["at"])
    start = (meal_at - VIEW_LEAD).replace(second=0, microsecond=0)
    end = meal_at + VIEW_LAG
    moments = set()
    at = start
    while at <= end:
        moments.add(at)
        at += timedelta(minutes=1)
    window = meal["window"]
    moments.update(parse_instant(window[key]) for key in ("auto_start", "auto_end", "start", "end"))
    for item in treatments:
        if start <= item["at"] <= end:
            moments.add(item["at"])
            if item.get("eventType") == "Exercise":
                moments.add(item["at"] + timedelta(minutes=int(item.get("duration") or 0)))
    # Only the CGM values near the view are needed for interpolation and trend.
    times = [row["at"] for row in entries]
    nearby = entries[max(0, bisect_left(times, start - timedelta(minutes=30))):
                     bisect_left(times, end + timedelta(minutes=30))]
    return {"meal_id": meal["id"], "limits": LIMITS,
            "readings": [_reading(nearby, moment, settings) for moment in sorted(moments)]}


def preview(payload: dict) -> dict:
    """What a candidate window counts, with the same half-open rule and full doses as the factor."""
    meal, _, entries, treatments = _meal(payload)
    _, settings = config.get_settings()
    meal_at = parse_instant(meal["at"])
    start = parse_instant(str(payload["start"]))
    end = parse_instant(str(payload["end"]))
    episode = {**meal, "at": meal_at}
    carbs, insulin = calculation.window_totals(episode, treatments, start, end)
    readings = {name: _reading(entries, moment, settings) for name, moment in (("start", start), ("end", end))}
    values = [readings[name]["value"] for name in ("start", "end")]
    try:
        settings_service.validate_manual_window(meal_at, start, end)
        problem = None
    except ValueError as error:
        problem = str(error)
    return {
        "start": readings["start"], "end": readings["end"],
        "delta_mgdl": (str(Decimal(values[1]) - Decimal(values[0])) if None not in values else None),
        "minutes": int((end - start) / timedelta(minutes=1)),
        "carbs_g": str(carbs), "rapid_units": str(insulin),
        "missing_glucose": None in values, "problem": problem,
    }
