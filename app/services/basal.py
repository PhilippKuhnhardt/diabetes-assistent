"""Basal doses: stored locally first, mirrored to Nightscout as notes."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation, episodes, nightscout
from app.store import basal_doses, config, mirror
from app.common import local_day_range, parse_instant, unit_text



def basal_event_id(record: dict) -> str | None:
    """App event ID of a basal note in Nightscout, or None for other treatments."""
    notes = str(record.get("notes") or "")
    if episodes.BASAL_MARKER not in notes:
        return None
    match = re.search(r"\[DAE:([a-f0-9-]{36})\]", notes)
    return match.group(1) if match else (str(record.get("identifier") or "") or None)


def basal_record(dose: dict) -> dict:
    """Nightscout copy of a basal dose: a note without an insulin field, so neither
    Nightscout's IOB nor the meal-factor model counts it as rapid insulin."""
    units = calculation.dec(dose["units"])
    note = str(dose.get("notes") or "").strip()
    text = f"Basal {unit_text(units)} IE" + (f" — {note}" if note else "")
    return {
        "eventType": "Note", "created_at": dose["at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "notes": f"{text} {episodes.BASAL_MARKER} [DAE:{dose['id']}]", "basal_units": float(units),
        "enteredBy": "Diabetes Diary", "identifier": dose["id"],
    }


def push_basal(dose: dict, remote: list[dict] | None = None) -> None:
    """Write a local basal dose to Nightscout unless it is already there."""
    if remote is None:
        _, settings = config.get_settings()
        day = dose["at"].astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
        start, end = local_day_range(day, settings["timezone"])
        _, remote = nightscout.fetch_range(start, end)
    if not any(basal_event_id(item) == dose["id"] for item in remote):
        nightscout.write_treatment(basal_record(dose))
    basal_doses.set_basal_synced(dose["id"])


def reconcile_basal(start: datetime, end: datetime, raw_treatments: list[dict], fresh: bool) -> tuple[list[dict], str | None]:
    """Local basal doses of a day. After a fresh Nightscout read, unsent doses are sent,
    doses deleted in Nightscout are dropped, and basal notes known only to Nightscout are adopted."""
    local = basal_doses.basal_doses_between(start, end)
    if not fresh:
        return local, None
    error = None
    remote = {}
    for item in raw_treatments:
        dose_id = basal_event_id(item)
        if dose_id:
            remote[dose_id] = item
    for dose in local:
        if dose["id"] in remote:
            if not dose["nightscout_synced"]:
                basal_doses.set_basal_synced(dose["id"])
        elif dose["nightscout_synced"]:
            basal_doses.delete_basal_dose(dose["id"])
        else:
            try:
                push_basal(dose, raw_treatments)
            except nightscout.NightscoutError as failure:
                error = str(failure)
    known = {dose["id"] for dose in local}
    for dose_id, item in remote.items():
        units = calculation.dec(item.get("basal_units") or 0)
        if dose_id in known or not units.is_finite() or not 0 < units <= 100:
            continue
        try:
            at = episodes.parse_at(item)
        except (ValueError, KeyError):
            continue
        basal_doses.save_basal_dose(dose_id, at, unit_text(units), synced=True)
    return basal_doses.basal_doses_between(start, end), error


def save_basal(payload: dict) -> dict:
    """Store a basal injection locally and, when connected, as a Nightscout note."""
    at = parse_instant(str(payload["at"]))
    units = calculation.dec(payload.get("units") or 0)
    if not units.is_finite() or not 0 < units <= 100 or units % Decimal("0.5"):
        raise ValueError("Basal muss zwischen 0,5 und 100 IE in 0,5er-Schritten liegen")
    dose_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
    note = str(payload.get("notes") or "").strip()[:500]
    _, settings = config.get_settings()
    day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
    basal_doses.save_basal_dose(dose_id, at, unit_text(units), note)
    dose = basal_doses.basal_dose(dose_id)
    error = None
    if nightscout.is_configured():
        try:
            push_basal(dose)
        except nightscout.NightscoutError as failure:
            error = str(failure)
    else:
        error = "Nightscout ist nicht verbunden"
    return {"id": dose_id, "day": day, "nightscout_synced": error is None, "nightscout_error": error}


def delete_basal(dose_id: str) -> None:
    dose = basal_doses.basal_dose(dose_id)
    if dose is None:
        raise ValueError("Basal-Eintrag nicht gefunden")
    if nightscout.is_configured():
        try:
            _, settings = config.get_settings()
            day = dose["at"].astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
            start, end = local_day_range(day, settings["timezone"])
            _, remote = nightscout.fetch_range(start, end)
            for item in remote:
                if basal_event_id(item) == dose_id and item.get("_id"):
                    nightscout.delete_treatment(str(item["_id"]))
                    mirror.delete_cached_treatment(str(item["_id"]))
        except nightscout.NightscoutError:
            # A dose Nightscout never received can still be removed locally.
            if dose["nightscout_synced"]:
                raise
    basal_doses.delete_basal_dose(dose_id)


def delete_basal_entry(payload: dict) -> dict:
    dose_id = payload.get("id")
    if not isinstance(dose_id, str) or not dose_id or len(dose_id) > 200:
        raise ValueError("Ungültiger Eintrag")
    delete_basal(dose_id)
    return {"id": dose_id}
