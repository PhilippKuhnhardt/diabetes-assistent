"""Dishes: the catalogue view with each dish's history, the entry form's dish chips, and recording
what an entry was. Outcomes come from the stored day results; nothing is calculated twice."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import dishes as rules
from app.common import local_zone, parse_instant
from app.store import config, dishes as store, marks, mirror

VIEW_DAYS = 120
VIEW_MAX_DAYS = 730
HISTORY_ROWS = 12


def _zone():
    _, settings = config.get_settings()
    return local_zone(settings["timezone"])


def _meal_rows(days: int) -> list[dict]:
    """Main meals of the last `days` days from the stored day results, with their dish."""
    from app.services import day_view
    zone = _zone()
    today = datetime.now(zone).date()
    first_at = mirror.first_cached_at()
    first = today - timedelta(days=days - 1)
    if first_at:
        first = max(first, parse_instant(first_at).astimezone(zone).date())
    excluded = marks.analysis_exclusions()
    rows = []
    day = first
    while first_at and day <= today:
        payload = day_view.day_payload(day.isoformat(), refresh=False)
        for meal in payload.get("meals", []):
            result = meal.get("calculation") or {}
            rows.append({
                "day": payload["day"], "meal": meal.get("meal"), "meal_id": meal["id"],
                "at": parse_instant(meal["at"]).astimezone(zone).isoformat(timespec="minutes") if meal.get("at") else "",
                "dish_id": (meal.get("dish") or {}).get("id"),
                "carbs_g": result.get("carbs_total"), "rapid_units": result.get("rapid_units_in_window"),
                "factor": result.get("factor_units_per_ke"),
                "g0_mgdl": result.get("g0"), "g_end_mgdl": result.get("g3"), "delta_mgdl": result.get("delta_mgdl"),
                "excluded": meal["id"] in excluded,
                "blocked_reason": None if result.get("factor_units_per_ke") else result.get("reason"),
                "place": (meal.get("place") or {}).get("name"),
                **{key: (meal.get("quality") or {}).get(key) for key in ("carbs_certainty", "fat_protein", "fast")},
            })
        day += timedelta(days=1)
    return rows


def _days(params: dict | None) -> int:
    try:
        days = int(((params or {}).get("days") or [VIEW_DAYS])[0])
    except (TypeError, ValueError):
        raise ValueError("Ungültiger Zeitraum") from None
    if not 1 <= days <= VIEW_MAX_DAYS:
        raise ValueError("Ungültiger Zeitraum")
    return days


def view(params: dict | None = None) -> dict:
    """The catalogue: every dish with its entries, typical carbs, outcome and recent main meals."""
    days = _days(params)
    meals = _meal_rows(days)
    records = store.dish_records()
    result = []
    for dish in store.all_dishes():
        own = [record for record in records if record["dish_id"] == dish["id"]]
        typical = rules.typical_carbs([record["carbs_g"] for record in own])
        rows = rules.history(meals, dish["id"])
        outcome = rules.outcome(rows, typical)
        result.append({
            **dish, "count": len(own), "typical_carbs": typical,
            "last_at": own[-1]["at"] if own else None,
            "outcome": outcome, "hint": rules.hint(len(own), typical, outcome),
            "history": [{key: value for key, value in row.items() if key != "dish_id"}
                        for row in rows[-HISTORY_ROWS:]][::-1],
        })
    result.sort(key=lambda dish: (dish["archived"], -(dish["count"]), dish["name"].casefold()))
    return {"days": days, "dishes": result,
            "categories": sorted({dish["category"] for dish in result if dish["category"]}, key=str.casefold)}


ACTIONS = ("create", "rename", "category", "archive", "merge", "delete")


def _id(payload: dict, key: str) -> int:
    try:
        return int(payload[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Gericht nicht gefunden") from None


def save(payload: dict) -> dict:
    """One change from the dish catalogue, then the fresh view."""
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError("Unbekannte Aktion")
    if action == "create":
        store.create_dish(rules.validate_name(payload.get("name")), rules.validate_category(payload.get("category")))
    elif action == "rename":
        store.update_dish(_id(payload, "id"), name=rules.validate_name(payload.get("name")))
    elif action == "category":
        store.update_dish(_id(payload, "id"), category=rules.validate_category(payload.get("category")))
    elif action == "archive":
        store.update_dish(_id(payload, "id"), archived=payload.get("archived") is True)
    elif action == "merge":
        store.merge_dish(_id(payload, "id"), _id(payload, "into_id"))
    else:
        store.delete_dish(_id(payload, "id"))
    return view()


def dish_id(payload: dict) -> int | None:
    """The dish chosen in a form: an id, a new or existing name (`dish_name`), or empty for none."""
    name = payload.get("dish_name")
    if name not in (None, ""):
        name = rules.validate_name(name)
        return store.dish_named(name) or store.create_dish(name)
    value = payload.get("dish_id")
    if value in (None, "", "none"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("Gericht nicht gefunden") from None


def record(event_id: str, at: datetime, payload: dict, carbs) -> dict | None:
    """Store what the entry was: its dish and description. Returns the dish (id, name) or None."""
    chosen = dish_id(payload)
    store.set_record(event_id, at, chosen, rules.validate_description(payload.get("description")),
                     float(carbs) if carbs is not None else None)
    if chosen is None:
        return None
    return next(({"id": dish["id"], "name": dish["name"]} for dish in store.all_dishes() if dish["id"] == chosen), None)
