"""Keep local basal doses and their Nightscout notes in step after a fresh Nightscout read."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import calculation, episodes
from app.common import local_day_range, unit_text
from app.ingestion import nightscout
from app.store import basal_doses, config


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
