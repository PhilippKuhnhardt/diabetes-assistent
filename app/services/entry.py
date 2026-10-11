"""The diary entry form: what a new entry would mean (preview) and saving it in one call."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app import calculation, episodes, split as split_bolus
from app.common import parse_instant
from app.ingestion import nightscout
from app.services import annotations, basal, day_view, diary, dose_help
from app.store import config, mirror

MODES = ("meal", "exercise", "basal")
DOSE_VARIANTS = ("normal", "cautious", "carbs")
# The planned second part of a split bolus, offered as its own option while it is open.
SPLIT_VARIANT = "split"
# The fields a shown suggestion depends on; saving a tapped suggestion re-checks them together with the
# shown option units. Raw totals and expected values are left out: what still acts decays every second,
# so they never match between showing and saving, while the rounded doses only change for a real reason.
DOSE_CHECK_KEYS = (
    "carbs_g", "glucose_at", "glucose_mgdl", "isf_mgdl_per_unit", "target_mgdl", "cautious_target_mgdl",
    "units_per_ke", "factor_source", "day_type", "activity_change_mgdl", "auto_units_per_ke",
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


def _finger(payload: dict) -> Decimal | None:
    """Blood glucose measured by hand in the form; empty means none, out of range raises."""
    raw = str(payload.get("finger_mgdl") if payload.get("finger_mgdl") is not None else "").strip()
    if raw == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or not 20 <= value <= 600:
        raise ValueError("Blutig gemessener BZ muss zwischen 20 und 600 mg/dL liegen")
    return value


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
            "current": current, "at": at, "treatments": treatments, "carbs": _carbs(payload),
            "finger": _finger(payload) if mode == "meal" else None}


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


def _dose_check(result: dict, options: list[dict]) -> str:
    shown = [[option.get(key) for key in ("variant", "units", "later_units")] for option in options]
    return hashlib.sha256(json.dumps([[result.get(key) for key in DOSE_CHECK_KEYS], shown],
                                     default=str).encode()).hexdigest()


def _split(context: dict, assignment: dict | None, payload: dict) -> dict | None:
    """The split-bolus chip: offered and preselected for a large main meal; the form can switch it off."""
    carbs, at = context["carbs"], context["at"]
    if context["mode"] != "meal" or not carbs or at is None:
        return None
    offer = split_bolus.offer(carbs, bool(assignment and assignment["snack"]), context["settings"])
    if offer is None:
        return None
    return {**offer, "active": payload.get("split") is not False,
            "due_at": (at + timedelta(minutes=offer["delay_minutes"])).isoformat()}


def _dose(context: dict, payload: dict, assignment: dict | None, sport: dict | None,
          split: dict | None = None) -> dict:
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
                                  "snack": bool(assignment and assignment["snack"]),
                                  "finger_mgdl": context["finger"], "finger_at": context["at"]})
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
        option = {"variant": variant, "units": units, "expected_mgdl": result.get(prefix + keys[variant][1])}
        if split and split["active"]:
            # The shown units are the first part; the expected value assumes the planned second part too.
            first, later = split_bolus.parts(Decimal(str(units)), split["first_percent"])
            option.update({"units": str(first), "full_units": str(units), "later_units": str(later),
                           "later_at": split["due_at"]})
        option["savable"] = Decimal(option["units"]) <= 100
        options.append(option)
    plan = result.get("split_plan")
    if plan and carbs == 0:
        units = Decimal(plan["units"])
        forecast = result.get("forecast_mgdl")
        options.insert(0, {"variant": SPLIT_VARIANT, "units": plan["units"], "due_at": plan["due_at"],
                           "expected_mgdl": (str(Decimal(forecast) - units * Decimal(result["isf_mgdl_per_unit"]))
                                             if forecast is not None else None),
                           "savable": units <= 100})
    # Without a fresh CGM value the form offers its field for blood glucose measured by hand.
    finger_offer = not result.get("glucose_fresh") or result.get("glucose_source") == "finger"
    return {**result, "options": options, "check": _dose_check(result, options), "finger_offer": finger_offer}


def _on_board(context: dict, dose: dict) -> dict | None:
    """Today's "Wirkt noch" card, whatever the form currently holds."""
    if context["day"] != context["today"]:
        return None
    if "on_board" in dose:
        return dose["on_board"]
    try:
        return dose_help.dose_help({"carbs": "0"}).get("on_board")
    except ValueError:
        return None


def preview(payload: dict) -> dict:
    """Everything the entry form shows for the values typed so far."""
    context = _context(payload)
    assignment = _assignment(context, payload)
    sport = _sport(context, assignment)
    current = context["current"]
    day_type_visible = (context["mode"] == "meal" and bool(current.get("day_type"))
                        and _before_breakfast_end(context))
    split = _split(context, assignment, payload)
    dose = _dose(context, payload, assignment, sport, split)
    return {
        "day": context["day"],
        "assignment": assignment and {**assignment, "at": assignment["at"].isoformat()},
        "sport": sport,
        "basal_units": str(_basal_units(context)),
        "day_type": ({"value": current["day_type"], "source": current["day_type_source"]}
                     if day_type_visible else None),
        "split": split,
        "dose": dose,
        "on_board": _on_board(context, dose),
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
    finger = context["finger"]
    if mode == "meal" and carbs <= 0 and insulin <= 0 and finger is None:
        raise ValueError("Bitte Kohlenhydrate, Insulin oder beides eintragen.")
    if context["at"] is None:
        raise ValueError("Zeit fehlt")
    if finger is not None:
        # Saved first so the dose re-check below and later suggestions see the same start value.
        event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
        diary.save_glucose_check(context["at"], finger, str(uuid.uuid5(uuid.UUID(event_id), "finger")))
        payload = {**payload, "event_id": event_id}
        if carbs <= 0 and insulin <= 0:
            day = context["at"].astimezone(context["zone"]).date().isoformat()
            return {"day": day, "kind": "finger", "event_id": event_id, "id": None, "split_units": None,
                    "carbs": "0", "insulin": "0", "finger_mgdl": str(finger), "basal": None,
                    "basal_error": None, "sport_marked": False, "sport_error": None,
                    "day_view": day_view.day_payload(day)}
    assignment = _assignment(context, payload)
    sport = _sport(context, assignment)
    basal_units = _basal_units(context) if payload.get("with_basal") else Decimal(0)
    consider_sport = bool(sport and payload.get("consider_sport"))
    sport_change = _sport_change(payload) if consider_sport else Decimal(0)
    if sport_change is None:
        raise ValueError("Der geschätzte Sporteinfluss liegt außerhalb des erlaubten Bereichs.")
    split = _split(context, assignment, payload) if carbs > 0 else None
    dose = (_dose(context, payload, assignment, sport, split)
            if payload.get("dose_check") or (split and split["active"]) else {})
    if payload.get("dose_check"):
        # A tapped suggestion must still match the current one (new CGM value, new dose, profile change).
        if dose.get("check") != payload["dose_check"]:
            raise ValueError(DOSE_CHANGED)
    split_units = None
    if split and split["active"] and context["day"] == context["today"]:
        # The second part completes the chosen suggestion (normal unless another was tapped).
        options = {option["variant"]: option for option in dose.get("options", [])}
        option = options.get(payload.get("dose_variant")) or options.get("normal") or options.get("carbs")
        if option and option.get("full_units") is not None:
            later = Decimal(option["full_units"]) - insulin
            split_units = str(later) if later > 0 else None
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
        **({"split_units": split_units, "split_delay_minutes": split["delay_minutes"]}
           if split_units and kind == "meal" else {}),
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
        "split_units": split_units if kind == "meal" else None,
        "carbs": str(carbs), "insulin": str(insulin), "finger_mgdl": str(finger) if finger is not None else None,
        "basal": basal_result and {**basal_result, "units": str(basal_units)}, "basal_error": basal_error,
        "sport_marked": consider_sport and sport_error is None, "sport_error": sport_error,
        "day_view": current,
    }
