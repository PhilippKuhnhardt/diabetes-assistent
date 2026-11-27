"""The diary entry form: what a new entry would mean (preview) and saving it in one call."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone

from app import calculation, core
from app.common import local_zone, parse_instant
from app.ingestion import nightscout
from app.services import basal, day_view, diary, dose_help, places
from app.store import config, marks
from app.i18n import tr

# The rules of the form (assignment, closing a meal, basal chip, split chip, dose options, slider, what a
# save stores) are in the Rust core (core/diabetes-core/src/entry.rs). This module reads what they need,
# hashes the dose check, and writes the entry.
DOSE_VARIANTS = tuple(core.call("entry.dose_variants"))
DOSE_CHANGED = tr("Empfehlung neu berechnet. Bitte den neuen Wert prüfen und neu antippen.")


class DoseChanged(ValueError):
    """The tapped suggestion's dose is no longer the current one; the page shows the new one."""


def _check(material) -> str | None:
    """The tapped-suggestion check: a hash of what the shown suggestion depends on (entry.rs DOSE_CHECK_KEYS)."""
    if material is None:
        return None
    return hashlib.sha256(json.dumps(material, default=str).encode()).hexdigest()


def _inputs(payload: dict) -> dict:
    """What entry.rs reads: the stored day, the settings, the place chips and, for today, the dose help's snapshot."""
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    _, settings = config.get_settings()
    zone = local_zone(settings["timezone"])
    now = datetime.now(timezone.utc)
    today = now.astimezone(zone).date().isoformat()
    # Today follows the local copy while the background job keeps it current; same rule as the dose help.
    current = day_view.day_payload(day, refresh=day == today and not day_view.mirror_current(day))
    at = parse_instant(str(payload["at"])) if payload.get("at") else None
    place = place_id = None
    carbs = core.call("entry.carbs", payload=payload)
    if payload.get("mode", "meal") == "meal" and carbs and carbs > 0:
        place = places.chips(payload, at)
        chosen = payload.get("place_id")
        # The place the entry will be saved with: the chosen chip ("none" = no place), else the suggested one.
        place_id = places.place_id(chosen) if chosen is not None else place["place_id"]
    return {
        "payload": payload, "settings": settings, "at": at, "day": day, "today": today,
        "treatments": current["treatments"],
        "meals": [{key: meal.get(key) for key in ("id", "at", "source_id", "window")} for meal in current["meals"]],
        "basal_today": bool(current["basal"]), "day_type": current.get("day_type"),
        "day_type_source": current.get("day_type_source"), "place": place, "place_id": place_id,
        "snapshot": dose_help.snapshot({}, now) if day == today else None,
    }


def preview(payload: dict) -> dict:
    """Everything the entry form shows for the values typed so far."""
    result = core.call("entry.preview", **_inputs(payload))
    dose = result["dose"]
    if "check_material" in dose:
        dose["check"] = _check(dose.pop("check_material"))
    return result


def save(payload: dict) -> dict:
    """Save a diary entry with the basal riding along in one call."""
    inputs = _inputs(payload)
    plan = core.call("entry.save_plan", **inputs)
    at = inputs["at"]
    finger = calculation.dec(plan["finger_mgdl"]) if plan["finger_mgdl"] is not None else None
    if finger is not None:
        # Saved first so later suggestions see the same start value.
        event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
        diary.save_glucose_check(at, finger, str(uuid.uuid5(uuid.UUID(event_id), "finger")))
        payload = {**payload, "event_id": event_id}
        if plan["kind"] == "finger":
            day = plan["day"]
            return {"day": day, "kind": "finger", "event_id": event_id, "id": None, "split_units": None,
                    "carbs": "0", "insulin": "0", "finger_mgdl": str(finger), "basal": None,
                    "basal_error": None,
                    "day_view": day_view.with_checks(day_view.day_payload(day))}
    if plan["tapped"] and _check(plan["check_material"]) != payload["dose_check"] and not plan["same_dose"]:
        # A tapped suggestion must still match the current one (new CGM value, new dose, profile change).
        # Newer data that leaves the tapped dose as it was (e.g. only a new CGM reading) still saves it.
        raise DoseChanged(DOSE_CHANGED)
    kind, carbs, insulin = plan["kind"], calculation.dec(plan["carbs"]), calculation.dec(plan["insulin"])
    split_units = plan["split_units"]
    saved = diary.save_diary_treatment({
        "kind": kind, "at": at.isoformat(), "carbs": str(carbs), "insulin": str(insulin),
        "sport_kind": payload.get("sport_kind"), "duration_minutes": payload.get("duration_minutes") or 0,
        "notes": payload.get("notes") or "", "main_meal": plan["main_meal"],
        "carbs_certainty": payload.get("carbs_certainty") if carbs > 0 else None,
        "fat_protein": carbs > 0 and payload.get("fat_protein") is True,
        "alcohol": carbs > 0 and payload.get("alcohol") is True,
        "fast": carbs > 0 and payload.get("fast") is True,
        "event_id": payload.get("event_id"),
        **({key: payload[key] for key in ("dish_id", "dish_name", "description", "agent_session", "portion") if key in payload}
           if carbs > 0 else {}),
        **({"split_units": split_units, "split_delay_minutes": plan["split_delay_minutes"]} if split_units else {}),
        **({"fpe_surcharge": plan["fpe_surcharge"]} if plan["fpe_surcharge"] else {}),
    })
    day = saved["day"]
    if plan["close"]:
        # The preceding meal ends at this snack, exactly like "Zeitraum anpassen" in "Verlauf".
        marks.set_analysis_window(plan["close"]["meal_id"], parse_instant(plan["close"]["start"]),
                                  parse_instant(plan["close"]["end"]))
    if carbs > 0:
        places.record(saved["event_id"], at, payload)
    basal_units = calculation.dec(plan["basal_units"])
    basal_result = basal_error = None
    if basal_units > 0:
        try:
            basal_result = basal.save_basal({"at": at.isoformat(), "units": str(basal_units),
                                             "event_id": payload.get("basal_event_id")})
        except (ValueError, nightscout.NightscoutError) as error:
            basal_error = str(error)
    current = day_view.day_payload(day)
    marker = f"[DAE:{saved['event_id']}]"
    treatment = next((item for item in current["treatments"] if marker in str(item.get("notes") or "")), None)
    if treatment and plan["sport_drop"] is not None:
        marks.set_sport_session_effect(treatment["id"], plan["sport_drop"])
        current = day_view.day_payload(day, refresh=False)
    if treatment and plan["sport_minutes"] is not None:
        marks.set_sport_session_minutes(treatment["id"], plan["sport_minutes"])
        current = day_view.day_payload(day, refresh=False)
    return {
        "day": day, "kind": kind, "event_id": saved["event_id"], "id": treatment and treatment["id"],
        "split_units": split_units,
        "carbs": str(carbs), "insulin": str(insulin), "finger_mgdl": str(finger) if finger is not None else None,
        "basal": basal_result and {**basal_result, "units": str(basal_units)}, "basal_error": basal_error,
        "day_view": day_view.with_checks(current),
    }
