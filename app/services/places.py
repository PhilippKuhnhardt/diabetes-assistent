"""Eating places: the management view, the entry form's place chips, and recording an entry's place."""
from __future__ import annotations

from datetime import datetime, timezone

from app import places as rules
from app.store import places as store


def view() -> dict:
    """Places with their spots and entry counts."""
    spots = store.all_spots()
    counts = store.place_counts()
    return {
        "places": [{**place, "count": counts.get(place["id"], 0),
                    "spots": [{key: spot[key] for key in ("id", "lat", "lon")}
                              for spot in spots if spot["place_id"] == place["id"]]}
                   for place in store.all_places()],
        "max_accuracy_m": rules.MAX_ACCURACY_M,
    }


ACTIONS = ("create", "rename", "quick", "fallback", "delete", "add_spot", "delete_spot")


def _id(payload: dict, key: str) -> int:
    try:
        return int(payload[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Ort nicht gefunden") from None


def save(payload: dict) -> dict:
    """One change from the place management, then the fresh view."""
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError("Unbekannte Aktion")
    if action == "create":
        store.create_place(rules.validate_name(payload.get("name")))
    elif action == "rename":
        store.rename_place(_id(payload, "id"), rules.validate_name(payload.get("name")))
    elif action == "quick":
        store.set_quick(_id(payload, "id"), payload.get("quick") is True)
    elif action == "fallback":
        store.set_fallback(_id(payload, "id"), payload.get("fallback") is True)
    elif action == "delete":
        store.delete_place(_id(payload, "id"))
    elif action == "add_spot":
        store.add_spot(_id(payload, "place_id"), rules.validate_spot(payload))
    else:
        store.delete_spot(_id(payload, "spot_id"))
    return view()


def place_id(value) -> int | None:
    """The place chosen in a form: an id, or empty for none."""
    if value in (None, "", "none"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("Ort nicht gefunden") from None


def chips(payload: dict, at: datetime | None) -> dict:
    """The entry form's place row: every place, the one the position suggests, and why."""
    places = store.all_places()
    position = rules.parse_position(payload.get("position"))
    fallback = next((place["id"] for place in places if place["fallback"]), None)
    suggestion = rules.suggestion(position, at, datetime.now(timezone.utc), store.all_spots(), fallback)
    return {"options": [{"id": place["id"], "name": place["name"], "quick": place["quick"]} for place in places],
            **suggestion}


def record(event_id: str, at: datetime, payload: dict) -> None:
    """Store the chosen place; the position that suggested it is not kept."""
    store.set_entry_location(event_id, at, place_id(payload.get("place_id")))
