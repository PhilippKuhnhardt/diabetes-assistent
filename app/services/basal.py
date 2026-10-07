"""Basal doses: stored locally first, mirrored to Nightscout as notes."""
from __future__ import annotations

import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation
from app.common import local_day_range, parse_instant, unit_text
from app.ingestion import basal_sync, nightscout
from app.store import basal_doses, config, mirror



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
            basal_sync.push_basal(dose)
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
                if basal_sync.basal_event_id(item) == dose_id and item.get("_id"):
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
