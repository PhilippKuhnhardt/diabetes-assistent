"""The manual window editor: CGM readings to drag over and the totals of a candidate window.

The rules (limits, which instants the handles snap to, readings, totals) are in the Rust core
(core/diabetes-core/src/window.rs); this module reads the stored day and settings for it.
"""
from __future__ import annotations

from datetime import date

from app import core
from app.common import parse_instant
from app.services import day_view
from app.store import config
from app.i18n import tr


def _inputs(day: str, meal_id) -> dict:
    """The meal of the stored day with the day's readings and treatments, and the settings."""
    date.fromisoformat(day)
    current = day_view.day_payload(day, refresh=False)
    meal = next((row for row in current["meals"] if row["id"] == meal_id), None)
    if meal is None:
        raise ValueError(tr("Mahlzeit nicht gefunden"))
    _, settings = config.get_settings()
    return {"meal": meal, "entries": current["entries"], "treatments": current["treatments"], "settings": settings}


def editor(params: dict) -> dict:
    """Readings for every minute the handles can reach, plus the instants they snap to."""
    payload = {key: values[0] for key, values in params.items()}
    return core.call("window.editor", **_inputs(str(payload.get("day", "")), payload.get("meal_id")))


def preview(payload: dict) -> dict:
    """What a candidate window counts, with the same half-open rule and full doses as the factor."""
    inputs = _inputs(str(payload.get("day", "")), payload.get("meal_id"))
    return core.call("window.preview", start=parse_instant(str(payload["start"])),
                     end=parse_instant(str(payload["end"])), **inputs)
