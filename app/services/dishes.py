"""Dishes: the catalogue view with each dish's history, the entry form's dish chips, and recording
what an entry was. Outcomes come from the stored day results; nothing is calculated twice."""
from __future__ import annotations

from datetime import datetime, timedelta

from app import dishes as rules
from app.common import local_zone, parse_instant
from app.store import agent as agent_store, config, dishes as store, marks, mirror

VIEW_DAYS = 120
VIEW_MAX_DAYS = 730
HISTORY_ROWS = 12


def _zone():
    _, settings = config.get_settings()
    return local_zone(settings["timezone"])


def _local(value, zone) -> str:
    instant = value if isinstance(value, datetime) else parse_instant(str(value))
    return instant.astimezone(zone).isoformat(timespec="minutes")


def _rows(days: int) -> tuple[list[dict], list[dict]]:
    """Main meals and carb entries of the last `days` days from the stored day results. Meals carry
    their dish; entries carry dish, description and place and name the main meal they belong to."""
    from app.services import day_view
    zone = _zone()
    today = datetime.now(zone).date()
    first_at = mirror.first_cached_at()
    first = today - timedelta(days=days - 1)
    if first_at:
        first = max(first, parse_instant(first_at).astimezone(zone).date())
    excluded = marks.analysis_exclusions()
    meals, entries = [], []
    day = first
    while first_at and day <= today:
        payload = day_view.day_payload(day.isoformat(), refresh=False)
        kinds = {}
        for meal in payload.get("meals", []):
            result = meal.get("calculation") or {}
            kinds[meal["id"]] = meal.get("meal")
            meals.append({
                "day": payload["day"], "meal": meal.get("meal"), "meal_id": meal["id"],
                "at": _local(meal["at"], zone) if meal.get("at") else "",
                "dish_id": (meal.get("dish") or {}).get("id"),
                "carbs_g": result.get("carbs_total"), "rapid_units": result.get("rapid_units_in_window"),
                "factor": result.get("factor_units_per_ke"),
                "g0_mgdl": result.get("g0"), "g_end_mgdl": result.get("g3"), "delta_mgdl": result.get("delta_mgdl"),
                "excluded": meal["id"] in excluded,
                "blocked_reason": None if result.get("factor_units_per_ke") else result.get("reason"),
                "place": (meal.get("place") or {}).get("name"),
                **{key: (meal.get("quality") or {}).get(key) for key in ("carbs_certainty", "fat_protein", "fast")},
            })
        for item in payload.get("timeline", []):
            try:
                carbs = float(item.get("carbs") or 0)
            except (TypeError, ValueError):
                carbs = 0
            if carbs <= 0:
                continue
            entries.append({
                "day": payload["day"], "at": _local(item["at"], zone), "kind": item.get("kind"),
                "meal": kinds.get(item.get("meal_id")), "meal_id": item.get("meal_id"), "event_id": item.get("event_id"),
                "carbs_g": carbs, "dish": (item.get("dish") or {}).get("name"), "description": item.get("description") or "",
                "place": (item.get("place") or {}).get("name"), "carbs_certainty": item.get("carbs_certainty"),
                "fat_protein": item.get("fat_protein"), "fast": item.get("fast"),
            })
        day += timedelta(days=1)
    return meals, entries


def _meal_rows(days: int) -> list[dict]:
    return _rows(days)[0]


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


# The agent's lookups (user request 2026-11-16: the model gets tools to decide well rather than rules).
SEARCH_ROWS = 40


def _outcome(meal: dict | None) -> dict | None:
    if not meal:
        return None
    return {key: meal.get(key) for key in ("factor", "reference_factor", "ratio", "carbs_g", "rapid_units",
                                           "g0_mgdl", "g_end_mgdl", "delta_mgdl", "excluded", "blocked_reason")}


SAID_MAX = 600


def _agent_notes(event_ids: list[str]) -> dict[str, dict]:
    """What the meal agent knew about each entry: its items (name, amount, carbs) and Philipp's own words in
    that estimate (user report 2026-11-25: package values per 100 g and the weight per piece were only in his
    words, so a later estimate of the same waffle could not find them)."""
    notes = {}
    for event_id, session in agent_store.sessions_for([event_id for event_id in event_ids if event_id]).items():
        estimate = session.get("estimate") or {}
        said = " | ".join(turn["text"] for turn in session.get("messages") or []
                          if turn.get("role") == "user" and turn.get("text") and turn["text"] != "(nur Fotos)")
        note = {"items": [{key: item.get(key) for key in ("name", "amount", "carbs_g")} for item in estimate.get("items") or []],
                "philipp_said": said[:SAID_MAX]}
        portion = estimate.get("portion")
        if portion not in (None, 1, 1.0):
            note["portion"] = portion
        notes[event_id] = note
    return notes


def lookup_meals(query: str = "", meal: str | None = None, place: str | None = None, dish: str | None = None,
                 days: int = VIEW_DAYS) -> dict:
    """Past carb entries, newest first, filtered by words (all must appear in dish, description, the agent's
    items or Philipp's words to it), meal kind, place and dish, each with the outcome of the main meal it
    belongs to and what the agent knew about it."""
    days = max(1, min(int(days or VIEW_DAYS), VIEW_MAX_DAYS))
    meals, entries = _rows(days)
    by_id = {row["meal_id"]: row for row in meals}
    notes = _agent_notes([entry["event_id"] for entry in entries])
    words = [word.casefold() for word in str(query or "").split()]
    found = []
    for entry in reversed(entries):
        note = notes.get(entry["event_id"]) or {}
        text = " ".join([entry["dish"] or "", entry["description"], note.get("philipp_said") or "",
                         *(str(item.get("name") or "") for item in note.get("items") or [])]).casefold()
        if any(word not in text for word in words):
            continue
        if meal and entry["meal"] != meal:
            continue
        if place and (entry["place"] or "").casefold() != str(place).casefold():
            continue
        if dish and (entry["dish"] or "").casefold() != str(dish).casefold():
            continue
        found.append(entry)
    shown = []
    for entry in found[:SEARCH_ROWS]:
        main = by_id.get(entry["meal_id"])
        note = notes.get(entry["event_id"])
        shown.append({**{key: value for key, value in entry.items() if key not in ("meal_id", "event_id")},
                      **({"agent": note} if note else {}),
                      "meal_outcome": _outcome(rules.with_ratio(meals, main) if main else None)})
    return {"days": days, "total": len(found), "entries": shown}
