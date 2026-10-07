"""The diary entry form: what a new entry would mean (preview) and saving it in one call."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app import calculation, episodes
from app.common import parse_instant
from app.ingestion import nightscout
from app.services import annotations, basal, day_view, diary, dose_help
from app.store import config, mirror

MODES = ("meal", "exercise", "basal")
DOSE_VARIANTS = ("normal", "cautious", "carbs")
# The fields a shown suggestion depends on; saving a tapped suggestion re-checks them.
DOSE_CHECK_KEYS = (
    "carbs_g", "glucose_at", "glucose_mgdl", "isf_mgdl_per_unit", "target_mgdl", "cautious_target_mgdl",
    "units_per_ke", "factor_source", "day_type", "activity_change_mgdl", "total_units", "cautious_total_units",
    "entry_units", "cautious_entry_units", "expected_glucose_mgdl", "cautious_expected_glucose_mgdl",
    "auto_units_per_ke", "auto_total_units", "auto_entry_units", "auto_expected_glucose_mgdl",
    "auto_cautious_total_units", "auto_cautious_entry_units", "auto_cautious_expected_glucose_mgdl",
    "carbs_entry_units", "carbs_expected_glucose_mgdl", "auto_carbs_entry_units", "auto_carbs_expected_glucose_mgdl",
    "recent_rapid_doses")
DOSE_CHANGED = "Empfehlung geändert. Bitte den neuen Wert prüfen und erneut speichern."


def _carbs(payload: dict) -> Decimal | None:
    """Empty carbohydrates mean a correction without food; invalid input yields None."""
    raw = str(payload.get("carbs") if payload.get("carbs") is not None else "").strip()
    if raw == "":
        return Decimal(0)
    try:
        carbs = Decimal(raw)
    except InvalidOperation:
        return None
    return carbs if carbs.is_finite() and 0 <= carbs <= 500 else None


def _sport_change(payload: dict) -> Decimal | None:
    """The signed BG change typed for the sport (−40 lowers); None when out of range."""
    try:
        value = Decimal(str(payload.get("sport_effect_mgdl") or 0))
    except InvalidOperation:
        return None
    return value if value.is_finite() and -500 <= value <= 500 else None


def _context(payload: dict) -> dict:
    day = str(payload.get("day", ""))
    date.fromisoformat(day)
    mode = payload.get("mode", "meal")
    if mode not in MODES:
        raise ValueError("Ungültiger Eintragstyp")
    _, settings = config.get_settings()
    zone = ZoneInfo(settings["timezone"])
    today = datetime.now(zone).date().isoformat()
    if day == today:
        # Same freshness rule as the dose help, so the suggestion below reuses this sync.
        synced = mirror.synced_at(day)
        recent = bool(synced and datetime.now(timezone.utc) - parse_instant(synced) < timedelta(minutes=1))
        current = day_view.day_payload(day, refresh=not recent)
    else:
        current = day_view.day_payload(day, refresh=False)
    at = parse_instant(str(payload["at"])) if payload.get("at") else None
    treatments = [{**row, "at": parse_instant(row["at"])} for row in current["treatments"]]
    return {"day": day, "mode": mode, "settings": settings, "zone": zone, "today": today,
            "current": current, "at": at, "treatments": treatments, "carbs": _carbs(payload)}


def _assignment(context: dict, payload: dict) -> dict | None:
    """A new carb entry right after a main meal counts as its snack unless flipped to its own meal."""
    at, carbs = context["at"], context["carbs"]
    if context["mode"] != "meal" or at is None or not carbs or carbs <= 0:
        return None
    root = episodes.preceding_meal(at, context["treatments"], context["settings"])
    if root is None:
        return None
    return {"meal": root["meal"], "at": root["at"], "own_meal": episodes.meal_kind_at(at, context["settings"])[1],
            "snack": payload.get("as") != "meal"}


def _minute(context: dict) -> int | None:
    at = context["at"]
    if at is None:
        return None
    local = at.astimezone(context["zone"])
    return local.hour * 60 + local.minute


def _before_breakfast_end(context: dict) -> bool:
    minute = _minute(context)
    return minute is not None and minute < int(context["settings"]["breakfast_end_minute"])


def _sport(context: dict, assignment: dict | None) -> dict | None:
    """Sport that ended within the lookback before a main meal; a snack never gets it."""
    at, carbs, settings = context["at"], context["carbs"], context["settings"]
    if context["mode"] != "meal" or at is None or not carbs or carbs <= 0 or (assignment and assignment["snack"]):
        return None
    lookback = timedelta(minutes=int(settings["sport_lookback_minutes"]))
    events = [item for item in context["treatments"]
              if item.get("eventType") == "Exercise" and item.get("sport_type")
              and at - lookback <= item["at"] + timedelta(minutes=int(item.get("duration") or 0)) <= at]
    if not events:
        return None
    # Stored effects are drops; the form shows the signed BG change.
    change = -sum((calculation.dec(settings["sport_effects_mgdl"].get(item["sport_type"], 0)) for item in events),
                  Decimal(0))
    return {"events": [{"sport_type": item["sport_type"], "at": item["at"].isoformat()} for item in events],
            "suggested_change_mgdl": str(change)}


def _basal_units(context: dict) -> Decimal:
    """Before breakfast end, while the day has no basal yet, the usual dose rides along."""
    units = calculation.dec(context["settings"].get("basal_default_units", 17))
    if (context["mode"] != "meal" or not units.is_finite() or units <= 0 or context["current"]["basal"]
            or not _before_breakfast_end(context)):
        return Decimal(0)
    return units


def _dose_check(result: dict) -> str:
    return hashlib.sha256(json.dumps([result.get(key) for key in DOSE_CHECK_KEYS], default=str).encode()).hexdigest()


def _dose(context: dict, payload: dict, assignment: dict | None, sport: dict | None) -> dict:
    """The suggestion for today, with the three options already resolved for display."""
    carbs = context["carbs"]
    if context["mode"] != "meal" or carbs is None:
        return {"message": ""}
    if context["day"] != context["today"]:
        return {"message": "Eine aktuelle Empfehlung gibt es nur für den heutigen Tag." if carbs > 0 else ""}
    change = _sport_change(payload) if sport and payload.get("consider_sport") else Decimal(0)
    if change is None:
        return {"message": "Der geschätzte Sporteinfluss liegt außerhalb des erlaubten Bereichs."}
    result = dose_help.dose_help({"carbs": str(carbs), "activity_change_mgdl": str(change),
                                  "snack": bool(assignment and assignment["snack"])})
    # With carbs every option uses the calculated factor; until it has enough meals the
    # breakfast day-type median or Nightscout's I:C stands in.
    prefix = "auto_" if carbs > 0 and result.get("auto_units_per_ke") is not None else ""
    keys = {"normal": ("entry_units", "expected_glucose_mgdl"),
            "cautious": ("cautious_entry_units", "cautious_expected_glucose_mgdl"),
            "carbs": ("carbs_entry_units", "carbs_expected_glucose_mgdl")}
    options = []
    for variant in DOSE_VARIANTS:
        if variant == "carbs" and carbs <= 0:
            continue
        units = result.get(prefix + keys[variant][0])
        if units is None:
            continue
        options.append({"variant": variant, "units": units, "expected_mgdl": result.get(prefix + keys[variant][1]),
                        "savable": Decimal(str(units)) <= 100})
    return {**result, "options": options, "check": _dose_check(result)}


def preview(payload: dict) -> dict:
    """Everything the entry form shows for the values typed so far."""
    context = _context(payload)
    assignment = _assignment(context, payload)
    sport = _sport(context, assignment)
    current = context["current"]
    day_type_visible = (context["mode"] == "meal" and bool(current.get("day_type"))
                        and _before_breakfast_end(context))
    return {
        "day": context["day"],
        "assignment": assignment and {**assignment, "at": assignment["at"].isoformat()},
        "sport": sport,
        "basal_units": str(_basal_units(context)),
        "day_type": ({"value": current["day_type"], "source": current["day_type_source"]}
                     if day_type_visible else None),
        "dose": _dose(context, payload, assignment, sport),
    }


def save(payload: dict) -> dict:
    """Save a diary entry with the basal riding along and the confirmed sport in one call."""
    context = _context(payload)
    mode = context["mode"]
    if mode == "basal":
        raise ValueError("Basal wird über die Basal-Eingabe gespeichert")
    carbs = context["carbs"] if mode == "meal" else Decimal(0)
    if carbs is None:
        raise ValueError("KH oder Insulin außerhalb des erlaubten Bereichs")
    insulin = calculation.dec(payload.get("insulin") or 0) if mode == "meal" else Decimal(0)
    if mode == "meal" and carbs <= 0 and insulin <= 0:
        raise ValueError("Bitte Kohlenhydrate, Insulin oder beides eintragen.")
    if context["at"] is None:
        raise ValueError("Zeit fehlt")
    assignment = _assignment(context, payload)
    sport = _sport(context, assignment)
    basal_units = _basal_units(context) if payload.get("with_basal") else Decimal(0)
    consider_sport = bool(sport and payload.get("consider_sport"))
    sport_change = _sport_change(payload) if consider_sport else Decimal(0)
    if sport_change is None:
        raise ValueError("Der geschätzte Sporteinfluss liegt außerhalb des erlaubten Bereichs.")
    if payload.get("dose_check"):
        # A tapped suggestion must still match the current one (new CGM value, new dose, profile change).
        if _dose(context, payload, assignment, sport).get("check") != payload["dose_check"]:
            raise ValueError(DOSE_CHANGED)
    if mode == "exercise":
        kind = "exercise"
    elif carbs > 0:
        kind = "snack" if assignment and assignment["snack"] else "meal"
    else:
        kind = "correction"
    saved = diary.save_diary_treatment({
        "kind": kind, "at": context["at"].isoformat(), "carbs": str(carbs), "insulin": str(insulin),
        "sport_kind": payload.get("sport_kind"), "duration_minutes": payload.get("duration_minutes") or 0,
        "notes": payload.get("notes") or "", "main_meal": bool(assignment and not assignment["snack"]),
        "carbs_certainty": payload.get("carbs_certainty") if carbs > 0 else None,
        "fat_protein": carbs > 0 and payload.get("fat_protein") is True,
        "event_id": payload.get("event_id"),
    })
    day = saved["day"]
    basal_result = basal_error = None
    if basal_units > 0:
        try:
            basal_result = basal.save_basal({"at": context["at"].isoformat(), "units": str(basal_units),
                                             "event_id": payload.get("basal_event_id")})
        except (ValueError, nightscout.NightscoutError) as error:
            basal_error = str(error)
    current = day_view.day_payload(day)
    marker = f"[DAE:{saved['event_id']}]"
    treatment = next((item for item in current["treatments"] if marker in str(item.get("notes") or "")), None)
    sport_error = None
    if consider_sport:
        meal = next((item for item in current["meals"] if treatment and item["source_id"] == treatment["id"]), None)
        if meal is None:
            sport_error = ("Eintrag gespeichert, aber die Sportzuordnung konnte nicht gefunden werden. "
                           "Bitte im Mahlzeitenfaktor prüfen.")
        else:
            # Internally a sport effect is a drop; the form holds the signed BG change.
            annotations.set_sport_mark({"meal_id": meal["id"], "day": day, "status": "affected",
                                        "effect_mgdl": str(-sport_change)})
            current = day_view.day_payload(day, refresh=False)
    return {
        "day": day, "kind": kind, "event_id": saved["event_id"], "id": treatment and treatment["id"],
        "carbs": str(carbs), "insulin": str(insulin),
        "basal": basal_result and {**basal_result, "units": str(basal_units)}, "basal_error": basal_error,
        "sport_marked": consider_sport and sport_error is None, "sport_error": sport_error,
        "day_view": current,
    }
