"""Diary entries written to Nightscout."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from app import calculation, episodes, fpe
from app.common import local_day_range, local_zone, parse_instant, unit_text
from app.ingestion import nightscout
from app.services import annotations, basal, day_view, dishes, places
from app.store import agent as agent_store, config, dishes as dish_store, marks, mirror


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
        raise ValueError("Ohne Fett/Eiweiß-Schätzung des Agenten gibt es keinen berechneten Zuschlag")
    return fpe.plan(found)


def save_diary_treatment(payload: dict) -> dict:
    """Validate a standalone diary event and write it to Nightscout."""
    if not nightscout.is_configured():
        raise ValueError("Zuerst Nightscout verbinden")
    kind = str(payload["kind"])
    if kind not in {"meal", "snack", "carbs", "correction", "exercise"}:
        raise ValueError("Ungültiger Eintragstyp")
    at = parse_instant(str(payload["at"]))
    _, settings = config.get_settings()
    day = at.astimezone(local_zone(settings["timezone"])).date().isoformat()
    carbs = calculation.dec(payload.get("carbs") or 0)
    units = calculation.dec(payload.get("insulin") or 0)
    if not carbs.is_finite() or not units.is_finite() or not 0 <= carbs <= 500 or not 0 <= units <= 100:
        raise ValueError("KH oder Insulin außerhalb des erlaubten Bereichs")
    if kind in {"meal", "snack", "carbs"} and carbs <= 0:
        raise ValueError("Kohlenhydrate müssen positiv sein")
    if kind in {"correction", "exercise"} and carbs != 0:
        raise ValueError("Dieser Eintrag hat keine Kohlenhydrate")
    if ((kind in {"carbs", "exercise"} and units != 0)
            or (kind == "correction" and units <= 0)):
        raise ValueError("Ungültige Insulinmenge")
    duration = 0
    sport_kind = None
    if kind == "exercise":
        sport_kind = str(payload.get("sport_kind", ""))
        if sport_kind not in episodes.SPORT_LABELS:
            raise ValueError("Ungültige Sportart")
        duration = int(payload.get("duration_minutes", 0))
        if not 1 <= duration <= 1440:
            raise ValueError("Sportdauer muss zwischen 1 und 1440 Minuten liegen")
    note = str(payload.get("notes") or "").strip()[:500]
    record_id = str(payload.get("id") or "")
    existing = None
    if record_id:
        old_day = str(payload.get("original_day") or day)
        date.fromisoformat(old_day)
        start, end = local_day_range(old_day, settings["timezone"])
        _, records = nightscout.fetch_range(start, end)
        existing = next((item for item in records if str(item.get("_id")) == record_id), None)
        if existing is None:
            raise ValueError("Eintrag in Nightscout nicht gefunden")
        if existing.get("eventType") not in {"Meal Bolus", "Snack Bolus", "Carb Correction",
                                              "Correction Bolus", "Bolus", "Exercise"}:
            raise ValueError("Dieser Eintragstyp kann hier nicht bearbeitet werden")
    previous_id = str((existing or {}).get("identifier") or "")
    try:
        previous_id = str(uuid.UUID(previous_id))
    except ValueError:
        previous_id = ""
    event_id = previous_id or str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
    if kind == "exercise":
        event_type = "Exercise"
        label = episodes.SPORT_LABELS[sport_kind]
        notes = label + (f" — {note}" if note else "") + f" [DAS1:{sport_kind}] [DAE:{event_id}]"
    else:
        event_type = {"meal": "Meal Bolus", "snack": "Snack Bolus" if units > 0 else "Carb Correction",
                      "carbs": "Carb Correction", "correction": "Correction Bolus"}[kind]
        main_meal = kind == "meal" and (bool(payload.get("main_meal"))
                                         or "[DA2:meal]" in str((existing or {}).get("notes") or ""))
        marker = " [DA2:snack]" if kind == "snack" else " [DA2:meal]" if main_meal else ""
        certainty = payload.get("carbs_certainty")
        if payload.get("carbs_unsure") is True and not certainty:  # older clients
            certainty = "unsure"
        if certainty not in (None, "", *episodes.CERTAINTIES):
            raise ValueError("Unbekannte KH-Sicherheit")
        # "keep" leaves a mark as the edited entry has it (the Alkohol chip is gone since 2026-11-15, its old
        # marks stay when the meal card saves).
        flags = {flag: (tag in str((existing or {}).get("notes") or "") if payload.get(flag) == "keep"
                        else payload.get(flag) is True)
                 for flag, tag in episodes.QUALITY_MARKERS.items()}
        quality = (f" [DAQ:{certainty}]" if carbs > 0 and certainty else "") + "".join(
            " " + tag for flag, tag in episodes.QUALITY_MARKERS.items() if carbs > 0 and flags[flag])
        split = ""
        if kind == "meal" and payload.get("split_units") is not None:
            units_later = calculation.dec(payload["split_units"])
            delay = int(payload.get("split_delay_minutes") or 0)
            if not units_later.is_finite() or not 0 < units_later <= 100 or not 30 <= delay <= 300:
                raise ValueError("Ungültiger Split-Bolus")
            split = " " + episodes.split_tag(unit_text(units_later), delay)
        elif kind == "meal" and existing:
            kept = episodes.SPLIT_TAG.search(str(existing.get("notes") or ""))
            split = " " + kept.group(0) if kept else ""
        surcharge = ""
        # The surcharge belongs to each carb entry (app/on_board.py), so any of them can carry the calculated one.
        eats = carbs > 0
        if eats and "fpe_surcharge" not in payload and payload.get("surcharge_mode") in fpe.MODES:
            # The card switches a saved entry (meal or snack alike, user request 2026-11-21) between the static and the calculated surcharge; the
            # calculated one comes from the agent's estimate linked to the entry (app/fpe.py).
            payload = {**payload, "fpe_surcharge": _calculated_surcharge(event_id, settings, payload.get("agent_session"),
                                                                payload.get("portion"))
                       if payload["surcharge_mode"] == "fpe" else None}
        if eats and payload.get("fpe_surcharge"):
            value = payload["fpe_surcharge"]
            mgdl, minutes = calculation.dec(value["mgdl"]), int(value["minutes"])
            if not mgdl.is_finite() or not 0 <= mgdl <= 200 or not 60 <= minutes <= 600:
                raise ValueError("Ungültiger Fett/Eiweiß-Zuschlag")
            surcharge = " " + episodes.surcharge_tag(unit_text(mgdl.quantize(Decimal(1))), minutes)
        elif eats and existing and "fpe_surcharge" not in payload:
            kept = episodes.SURCHARGE_TAG.search(str(existing.get("notes") or ""))
            surcharge = " " + kept.group(0) if kept else ""
        notes = (note + marker + quality + split + surcharge + f" [DAE:{event_id}]").strip()
        legacy = episodes.TAG.search(str((existing or {}).get("notes") or ""))
        if kind == "meal" and legacy and legacy.group(3) == "meal":
            notes += " " + legacy.group(0)
    record = dict(existing or {})
    record.update({
        "eventType": event_type, "created_at": at.isoformat().replace("+00:00", "Z"),
        "notes": notes, "enteredBy": "Diabetes Diary", "identifier": event_id,
        "carbs": float(carbs), "insulin": float(units),
    })
    if kind == "meal" and not existing:  # also a [DA2:meal] entry when its kind is taken that day
        start, end = local_day_range(day, settings["timezone"])
        _, nearby = nightscout.fetch_range(start, end)
        classified = episodes.normalize_treatments(nearby + [record])
        episodes.group_diary_followups(classified, settings)
        candidate = next(item for item in classified if f"[DAE:{event_id}]" in item["notes"])
        if candidate["eventType"] != "Meal Bolus":
            record["eventType"] = candidate["eventType"]
            record["notes"] = (note + " [DA2:snack]" + quality + f" [DAE:{event_id}]").strip()
    if "date" in record:
        record["date"] = int(at.timestamp() * 1000)
    if kind == "exercise":
        record["duration"] = duration
    else:
        record.pop("duration", None)
    if existing:
        nightscout.update_treatment(record_id, record)
        start, end = local_day_range(day, settings["timezone"])
        _, confirmed = nightscout.fetch_range(start, end)
        saved = next((item for item in confirmed if str(item.get("_id")) == record_id), None)
        if (saved is None or saved.get("eventType") != event_type
                or calculation.dec(saved.get("carbs") or 0) != carbs
                or calculation.dec(saved.get("insulin") or 0) != units
                or str(saved.get("notes") or "") != notes):
            raise nightscout.NightscoutError("Nightscout hat die Änderung nicht bestätigt")
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


EDITABLE_KINDS = {"meal", "snack", "carbs", "correction", "exercise", "basal"}


def edit_entry(payload: dict) -> dict:
    """A saved entry changed on its card (web/ui/entry-card.js). The card sends what it shows, as the entry
    form does; the kind follows from the entry's kind (`original_kind`) and the new values, and every check
    lives here and in save_diary_treatment. Returns the entry's day and that day's view."""
    original = str(payload.get("original_kind") or "")
    if original not in EDITABLE_KINDS:
        raise ValueError("Dieser Eintragstyp kann hier nicht bearbeitet werden")
    record_id = payload.get("id")
    old_day = str(payload.get("original_day") or "")
    if not isinstance(record_id, str) or not 0 < len(record_id) <= 200:
        raise ValueError("Ungültiger Eintrag")
    date.fromisoformat(old_day)
    if original == "basal":
        return _replace_basal(record_id, payload)
    sport = original == "exercise"
    carbs = Decimal(0) if sport else calculation.dec(payload.get("carbs") or 0)
    units = Decimal(0) if sport else calculation.dec(payload.get("insulin") or 0)
    if not sport and carbs <= 0 and units <= 0:
        raise ValueError("Bitte Kohlenhydrate, Insulin oder beides eintragen.")
    if sport:
        kind = "exercise"
        _edit_sport_session(record_id, old_day, payload)
    elif carbs > 0:
        kind = "snack" if original == "snack" else "carbs" if original == "carbs" and units == 0 else "meal"
    else:
        kind = "correction"
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
        **({"agent_session": payload["agent_session"], "portion": payload.get("portion")}
           if payload.get("agent_session") else {}),
    })
    current = day_view.day_payload(saved["day"])
    row = next((item for item in current["timeline"] if item["id"] == record_id), None)
    if row is None or calculation.dec(row.get("carbs") or 0) != carbs or calculation.dec(row.get("insulin") or 0) != units:
        raise ValueError("Die Änderung ist in Nightscout noch nicht sichtbar. Bitte Eintrag aktualisieren.")
    return {"day": saved["day"], "day_view": day_view.with_checks(current)}


def _edit_sport_session(record_id: str, day: str, payload: dict) -> None:
    """The session's own effect and duration (null = its kind's setting), written before the Nightscout update:
    right after it the day's cached copy may not hold the session yet. Only a change is written."""
    duration = payload.get("duration_minutes")
    if not isinstance(duration, (int, float)) or not 1 <= duration <= 1440:
        raise ValueError("Bitte eine Dauer zwischen 1 und 1440 Minuten eintragen.")
    change, minutes = payload.get("sport_effect_change_mgdl"), payload.get("sport_minutes")
    if change is not None and not (isinstance(change, int) and not isinstance(change, bool) and abs(change) <= 500):
        raise ValueError("Bitte eine BZ-Wirkung zwischen −500 und +500 mg/dL eintragen.")
    if minutes is not None and not (isinstance(minutes, (int, float)) and 30 <= minutes <= 1440):
        raise ValueError("Die Wirkdauer muss zwischen 0,5 und 24 h liegen.")
    effects, durations = marks.sport_session_effects(), marks.sport_session_minutes()
    changes = {}
    # The API stores a session's effect as a glucose drop; the card shows the signed change.
    if change is None and record_id in effects:
        changes["reset"] = True
    elif change is not None and (record_id not in effects or calculation.dec(effects[record_id]) != -change):
        changes["effect_mgdl"] = -change
    if minutes != durations.get(record_id):
        changes["minutes"] = minutes
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
        raise ValueError("Zuerst Nightscout verbinden")
    if not mgdl.is_finite() or not 20 <= mgdl <= 600:
        raise ValueError("Blutzucker muss zwischen 20 und 600 mg/dL liegen")
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
        raise ValueError("Zuerst Nightscout verbinden")
    record_id = payload.get("id")
    day = payload.get("day")
    if not isinstance(record_id, str) or not record_id or len(record_id) > 200 or not isinstance(day, str):
        raise ValueError("Ungültiger Eintrag")
    date.fromisoformat(day)
    _, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    _, treatments = nightscout.fetch_range(start, end)
    found = next((item for item in treatments if item.get("_id") == record_id), None)
    if found is None:
        raise ValueError("Eintrag in Nightscout nicht gefunden")
    nightscout.delete_treatment(record_id)
    if episodes.event_id(found):
        dish_store.delete_record(episodes.event_id(found))
    mirror.delete_cached_treatment(record_id)
    return {"id": record_id}
