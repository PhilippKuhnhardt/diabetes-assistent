"""Diary entries written to Nightscout."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from app import calculation, core, episodes, fpe
from app.common import local_day_range, local_zone, parse_instant
from app.ingestion import nightscout
from app.services import annotations, basal, day_view, dishes, places
from app.store import agent as agent_store, config, dishes as dish_store, marks, mirror
from app.i18n import tr

KINDS = {"meal", "snack", "carbs", "correction", "exercise"}


def ensure_not_duplicate(day: str, event_id: str) -> bool:
    _, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    _, existing = nightscout.fetch_range(start, end)
    return any(item.get("identifier") == event_id or f"[DAE:{event_id}]" in str(item.get("notes") or "") for item in existing)


def _calculated_surcharge(event_id: str, settings: dict, session_id: str | None = None, portion=None) -> dict:
    """The calculated surcharge from the agent's estimate: the session the save links (an estimate improved
    on the card), else the one already linked to the entry, for the portion taken over."""
    from app.services import agent
    session = None
    if session_id:
        try:
            session = agent_store.session(str(uuid.UUID(str(session_id))))
        except ValueError:
            session = None
    session = session or agent_store.sessions_for([event_id]).get(event_id)
    found = fpe.estimate(agent.scaled((session or {}).get("estimate"), agent.portion_value(portion)), settings)
    if found is None:
        raise ValueError(tr("Ohne Fett/Eiweiß-Schätzung des Agenten gibt es keinen berechneten Zuschlag"))
    return fpe.plan(found)


def save_diary_treatment(payload: dict) -> dict:
    """Write a standalone diary event to Nightscout; its record and every check are core diary.rs."""
    if not nightscout.is_configured():
        raise ValueError(tr("Zuerst Nightscout verbinden"))
    if str(payload["kind"]) not in KINDS:
        raise ValueError(tr("Ungültiger Eintragstyp"))
    at = parse_instant(str(payload["at"]))
    _, settings = config.get_settings()
    day = at.astimezone(local_zone(settings["timezone"])).date().isoformat()
    record_id = str(payload.get("id") or "")
    existing = None
    if record_id:
        old_day = str(payload.get("original_day") or day)
        date.fromisoformat(old_day)
        start, end = local_day_range(old_day, settings["timezone"])
        _, records = nightscout.fetch_range(start, end)
        existing = next((item for item in records if str(item.get("_id")) == record_id), None)
        if existing is None:
            raise ValueError(tr("Eintrag in Nightscout nicht gefunden"))
    previous_id = str((existing or {}).get("identifier") or "")
    try:
        previous_id = str(uuid.UUID(previous_id))
    except ValueError:
        previous_id = ""
    event_id = previous_id or str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
    carbs = calculation.dec(payload.get("carbs") or 0)
    if (carbs.is_finite() and carbs > 0 and "fpe_surcharge" not in payload
            and payload.get("surcharge_mode") in fpe.MODES):
        # The card switches a saved entry between the static and the calculated surcharge; the calculated
        # one comes from the agent's estimate linked to the entry (app/fpe.py).
        payload = {**payload, "fpe_surcharge": _calculated_surcharge(event_id, settings, payload.get("agent_session"),
                                                                    payload.get("portion"))
                   if payload["surcharge_mode"] == "fpe" else None}
    nearby = []
    if payload.get("kind") == "meal" and existing is None:  # a new main meal may become a snack
        start, end = local_day_range(day, settings["timezone"])
        _, nearby = nightscout.fetch_range(start, end)
    built = core.call("diary.record", payload=payload, settings=settings, at=at, event_id=event_id,
                      existing=existing, nearby=nearby)
    record, event_type, notes = built["record"], built["event_type"], built["notes"]
    carbs, units = calculation.dec(built["carbs"]), calculation.dec(built["insulin"])
    if existing:
        nightscout.update_treatment(record_id, record)
        start, end = local_day_range(day, settings["timezone"])
        _, confirmed = nightscout.fetch_range(start, end)
        saved = next((item for item in confirmed if str(item.get("_id")) == record_id), None)
        if (saved is None or saved.get("eventType") != event_type
                or calculation.dec(saved.get("carbs") or 0) != carbs
                or calculation.dec(saved.get("insulin") or 0) != units
                or str(saved.get("notes") or "") != notes):
            raise nightscout.NightscoutError(tr("Nightscout hat die Änderung nicht bestätigt"))
        mirror.delete_cached_treatment(record_id)
    elif not ensure_not_duplicate(day, event_id):
        nightscout.write_treatment(record)
    if "place_id" in payload and carbs > 0:
        # The edit dialog's place.
        places.record(event_id, at, {"place_id": payload["place_id"]})
    if carbs > 0 and ("dish_id" in payload or "dish_name" in payload or "description" in payload):
        # What the entry was (entry form, edit dialog, agent); a missing key keeps the stored value.
        stored = dish_store.records().get(event_id) or {}
        dishes.record(event_id, at, {
            "dish_id": payload["dish_id"] if "dish_id" in payload else stored.get("dish_id"),
            "dish_name": payload.get("dish_name"),
            "description": payload["description"] if "description" in payload else stored.get("description"),
        }, carbs)
    elif carbs > 0:
        dish_store.move_record(event_id, at, float(carbs))
    else:
        dish_store.delete_record(event_id)
    if payload.get("agent_session") and carbs > 0:
        from app.services import agent
        agent.link(payload["agent_session"], event_id, payload.get("portion"))
    return {"id": record_id or None, "event_id": event_id, "day": day}


def edit_entry(payload: dict) -> dict:
    """A saved entry changed on its card (web/ui/entry-card.js). The card sends what it shows, as the entry
    form does; the kind follows from the entry's kind (`original_kind`) and the new values (core diary.rs
    edit_kind, which also checks them). Returns the entry's day and that day's view."""
    old_day = str(payload.get("original_day") or "")
    edit = core.call("diary.edit_kind", payload=payload)
    date.fromisoformat(old_day)
    record_id, kind = payload["id"], edit["kind"]
    if kind == "basal":
        return _replace_basal(record_id, payload)
    sport = kind == "exercise"
    carbs, units = calculation.dec(edit["carbs"]), calculation.dec(edit["insulin"])
    if sport:
        _edit_sport_session(record_id, old_day, payload)
    food = {key: payload.get(key) for key in ("carbs_certainty", "place_id", "dish_name", "description")}
    saved = save_diary_treatment({
        "id": record_id, "original_day": old_day, "kind": kind, "at": payload.get("at"),
        "carbs": str(carbs), "insulin": str(units), "notes": payload.get("notes") or "",
        "sport_kind": payload.get("sport_kind"), "duration_minutes": payload.get("duration_minutes") if sport else 0,
        "fat_protein": carbs > 0 and payload.get("fat_protein") is True, "fast": carbs > 0 and payload.get("fast") is True,
        "alcohol": "keep",
        **({**food, "dish_id": None, "dish_name": str(food["dish_name"] or "").strip()} if carbs > 0 else {}),
        # Only an entry whose agent estimate gives a calculated surcharge offers the choice; without one the saved stays.
        **({"surcharge_mode": payload["surcharge_mode"]} if carbs > 0 and payload.get("surcharge_mode") else {}),
        # Without an agent estimate the card can change the fat/protein level (2026-11-26).
        **({"fpe_level": payload["fpe_level"]} if carbs > 0 and payload.get("fpe_level") in fpe.LEVELS else {}),
        **({"agent_session": payload["agent_session"], "portion": payload.get("portion")}
           if payload.get("agent_session") else {}),
    })
    current = day_view.day_payload(saved["day"])
    row = next((item for item in current["timeline"] if item["id"] == record_id), None)
    if row is None or calculation.dec(row.get("carbs") or 0) != carbs or calculation.dec(row.get("insulin") or 0) != units:
        raise ValueError(tr("Die Änderung ist in Nightscout noch nicht sichtbar. Bitte Eintrag aktualisieren."))
    return {"day": saved["day"], "day_view": day_view.with_checks(current)}


def _edit_sport_session(record_id: str, day: str, payload: dict) -> None:
    """The session's own effect and duration (null = its kind's setting), written before the Nightscout update:
    right after it the day's cached copy may not hold the session yet. Only a change is written."""
    changes = core.call("diary.sport_changes", payload=payload, effects=marks.sport_session_effects(),
                        durations=marks.sport_session_minutes(), record_id=record_id)
    if changes:
        annotations.set_sport_session({"id": record_id, "day": day, **changes})


def _replace_basal(record_id: str, payload: dict) -> dict:
    """An edited basal dose is saved new and the old one deleted, so Nightscout's note changes too."""
    saved = basal.save_basal({"at": payload.get("at"), "units": payload.get("units"), "notes": payload.get("notes"),
                              "event_id": payload.get("event_id")})
    try:
        basal.delete_basal(record_id)
    except (ValueError, nightscout.NightscoutError):
        try:
            basal.delete_basal(saved["id"])
        except (ValueError, nightscout.NightscoutError):
            pass  # The old dose's error is the one to show.
        raise
    return {"day": saved["day"], "day_view": None}


def save_glucose_check(at: datetime, mgdl: Decimal, event_id: str) -> None:
    """Blood glucose measured by hand, as a Nightscout BG Check. It is a start value for the dose
    help only; meal factors and the CGM curve never read it."""
    if not nightscout.is_configured():
        raise ValueError(tr("Zuerst Nightscout verbinden"))
    mgdl = calculation.dec(core.call("limits.finger_mgdl", mgdl=mgdl))
    _, settings = config.get_settings()
    day = at.astimezone(local_zone(settings["timezone"])).date().isoformat()
    if not ensure_not_duplicate(day, event_id):
        nightscout.write_treatment({
            "eventType": "BG Check", "created_at": at.isoformat().replace("+00:00", "Z"),
            "glucose": float(mgdl), "glucoseType": "Finger", "units": "mg/dl",
            "notes": f"[DAE:{event_id}]", "enteredBy": "Diabetes Diary", "identifier": event_id,
        })


def delete_treatment(payload: dict) -> dict:
    if not nightscout.is_configured():
        raise ValueError(tr("Zuerst Nightscout verbinden"))
    record_id = payload.get("id")
    day = payload.get("day")
    if not isinstance(record_id, str) or not record_id or len(record_id) > 200 or not isinstance(day, str):
        raise ValueError(tr("Ungültiger Eintrag"))
    date.fromisoformat(day)
    _, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    _, treatments = nightscout.fetch_range(start, end)
    found = next((item for item in treatments if item.get("_id") == record_id), None)
    if found is None:
        raise ValueError(tr("Eintrag in Nightscout nicht gefunden"))
    nightscout.delete_treatment(record_id)
    if episodes.event_id(found):
        dish_store.delete_record(episodes.event_id(found))
    mirror.delete_cached_treatment(record_id)
    return {"id": record_id}
