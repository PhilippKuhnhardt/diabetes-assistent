"""Diary entries written to Nightscout."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation, episodes
from app.common import MEALS, local_day_range, parse_instant, unit_text
from app.ingestion import nightscout
from app.services import day_view
from app.store import config, mirror


DOSAGE_ROLES = {"meal-dose", "correction", "snack"}


def record_with_tag(at: datetime, event_type: str, episode_id: str, meal: str, role: str, carbs: Decimal, units: Decimal, note: str, event_id: str) -> dict:
    marker = episodes.tag(episode_id, meal, role)
    text = (note.strip() + " " + marker + f" [DAE:{event_id}]").strip()
    return {
        "eventType": event_type, "created_at": at.isoformat().replace("+00:00", "Z"),
        "carbs": float(carbs), "insulin": float(units), "notes": text,
        "enteredBy": "Diabetes Analysis PoC", "identifier": event_id,
    }


def ensure_not_duplicate(day: str, event_id: str) -> bool:
    _, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    _, existing = nightscout.fetch_range(start, end)
    return any(item.get("identifier") == event_id or f"[DAE:{event_id}]" in str(item.get("notes") or "") for item in existing)


def save_diary_treatment(payload: dict) -> dict:
    """Validate a standalone diary event and write it to Nightscout."""
    if not nightscout.is_configured():
        raise ValueError("Zuerst Nightscout verbinden")
    kind = str(payload["kind"])
    if kind not in {"meal", "snack", "carbs", "correction", "exercise"}:
        raise ValueError("Ungültiger Eintragstyp")
    at = parse_instant(str(payload["at"]))
    _, settings = config.get_settings()
    day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
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
        quality = (f" [DAQ:{certainty}]" if carbs > 0 and certainty else "") + "".join(
            " " + tag for flag, tag in episodes.QUALITY_MARKERS.items() if carbs > 0 and payload.get(flag) is True)
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
        notes = (note + marker + quality + split + f" [DAE:{event_id}]").strip()
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
    return {"id": record_id or None, "event_id": event_id, "day": day}


def save_glucose_check(at: datetime, mgdl: Decimal, event_id: str) -> None:
    """Blood glucose measured by hand, as a Nightscout BG Check. It is a start value for the dose
    help only; meal factors and the CGM curve never read it."""
    if not nightscout.is_configured():
        raise ValueError("Zuerst Nightscout verbinden")
    if not mgdl.is_finite() or not 20 <= mgdl <= 600:
        raise ValueError("Blutzucker muss zwischen 20 und 600 mg/dL liegen")
    _, settings = config.get_settings()
    day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
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
    if not any(item.get("_id") == record_id for item in treatments):
        raise ValueError("Eintrag in Nightscout nicht gefunden")
    nightscout.delete_treatment(record_id)
    mirror.delete_cached_treatment(record_id)
    return {"id": record_id}


def save_episode(payload: dict) -> dict:
    if not nightscout.is_configured():
        raise ValueError("Zuerst Nightscout verbinden")
    at = parse_instant(str(payload["at"]))
    meal = str(payload["meal"])
    if meal not in MEALS:
        raise ValueError("Ungültige Mahlzeit")
    carbs = calculation.dec(payload["carbs"])
    insulin = calculation.dec(payload.get("insulin", 0))
    if not (0 < carbs <= 500 and 0 <= insulin <= 100):
        raise ValueError("Kohlenhydrate oder Insulin außerhalb des erlaubten Bereichs")
    note = str(payload.get("notes", ""))[:500]
    episode_id = str(uuid.UUID(str(payload.get("episode_id") or uuid.uuid4())))
    event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
    _, settings = config.get_settings()
    day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
    if not ensure_not_duplicate(day, event_id):
        record = record_with_tag(at, "Meal Bolus", episode_id, meal, "meal", carbs, insulin, note, event_id)
        nightscout.write_treatment(record)
    return {"episode_id": episode_id, "event_id": event_id, "day": day}


def save_exercise(payload: dict) -> dict:
    if not nightscout.is_configured():
        raise ValueError("Zuerst Nightscout verbinden")
    at = parse_instant(str(payload["at"]))
    kind = str(payload["kind"])
    if kind not in episodes.SPORT_LABELS:
        raise ValueError("Ungültige Sportart")
    duration_text = str(payload["duration_minutes"])
    if not re.fullmatch(r"\d+", duration_text):
        raise ValueError("Dauer muss eine ganze Minutenzahl sein")
    duration = int(duration_text)
    if not 1 <= duration <= 1440:
        raise ValueError("Dauer muss zwischen 1 und 1440 Minuten liegen")
    note = str(payload.get("notes", "")).strip()[:400]
    event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
    _, settings = config.get_settings()
    day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
    if not ensure_not_duplicate(day, event_id):
        label = episodes.SPORT_LABELS[kind]
        notes = f"{label}" + (f" — {note}" if note else "")
        notes += f" [DAS1:{kind}] [DAE:{event_id}]"
        nightscout.write_treatment({
            "eventType": "Exercise",
            "created_at": at.isoformat().replace("+00:00", "Z"),
            "duration": duration,
            "notes": notes,
            "enteredBy": "Diabetes Analysis PoC",
            "identifier": event_id,
        })
    return {"event_id": event_id, "day": day}


def save_dose(payload: dict) -> dict:
    if not nightscout.is_configured():
        raise ValueError("Zuerst Nightscout verbinden")
    at = parse_instant(str(payload["at"]))
    episode_id = str(uuid.UUID(str(payload["episode_id"])))
    meal = str(payload["meal"])
    role = str(payload["role"])
    if meal not in MEALS or role not in DOSAGE_ROLES:
        raise ValueError("Ungültiger Dosistyp")
    units = calculation.dec(payload["insulin"])
    if not 0 < units <= 100:
        raise ValueError("Insulin außerhalb des erlaubten Bereichs")
    carbs = calculation.dec(payload.get("carbs") or 0)
    if role == "snack" and not 0 < carbs <= 500:
        raise ValueError("Kohlenhydrate außerhalb des erlaubten Bereichs")
    if role != "snack" and carbs != 0:
        raise ValueError("Kohlenhydrate nur für Zwischenmahlzeiten erlaubt")
    event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
    _, settings = config.get_settings()
    day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
    if not ensure_not_duplicate(day, event_id):
        nearby_days = [(at.astimezone(ZoneInfo(settings["timezone"])).date() - timedelta(days=offset)).isoformat() for offset in (0, 1)]
        roots = [row for meal_day in nearby_days for row in day_view.day_payload(meal_day)["meals"]]
        if not any(row["id"] == episode_id and row["meal"] == meal and 0 <= (at - parse_instant(row["at"])).total_seconds() <= 3 * 3600 for row in roots):
            raise ValueError("Zugehörige Mahlzeit innerhalb von drei Stunden fehlt")
        event_type = {"correction": "Correction Bolus", "meal-dose": "Meal Bolus", "snack": "Snack Bolus"}[role]
        record = record_with_tag(at, event_type, episode_id, meal, role, carbs, units, "", event_id)
        nightscout.write_treatment(record)
    return {"event_id": event_id, "day": day}
