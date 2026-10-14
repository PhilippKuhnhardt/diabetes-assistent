"""One diary day: mirror from Nightscout, calculate its meals, build the view, keep the result."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app import calculation, episodes
from app import glucose as cgm
from app.common import json_safe, local_day_range, parse_instant
from app.ingestion import nightscout, sync
from app.services import settings as settings_service
from app.store import basal_doses as basal_store, config, marks, mirror, places as place_store, results
from app.analysis.night import night_analysis

# Any change to the app's code recalculates stored days once, so a stored result never
# outlives the code that produced it.
CODE_VERSION = hashlib.sha256(b"".join(
    path.read_bytes() for path in sorted(Path(__file__).resolve().parents[1].rglob("*.py")))).hexdigest()


def day_payload(day: str, refresh: bool = True) -> dict:
    """The diary day as the app shows it. With refresh it is first mirrored from Nightscout;
    the calculation is reused from SQLite while nothing it reads has changed."""
    status = sync.sync_day(day) if refresh else {}
    fingerprint = day_fingerprint(day)
    payload = results.day_result(day, fingerprint)
    if payload is None:
        payload = compute_day(day)
        results.save_day_result(day, fingerprint, payload)
    # Request-specific fields are never stored with the result.
    payload.update({
        "synced_at": mirror.synced_at(day),
        "connected": nightscout.is_configured(),
        "sync_error": status.get("sync_error"), "profile_sync_error": status.get("profile_sync_error"),
        "basal_sync_error": status.get("basal_sync_error"),
    })
    return payload


def day_fingerprint(day: str) -> str:
    """Hash of everything compute_day reads, plus the code that computes it."""
    _, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    return hashlib.sha256(json.dumps([CODE_VERSION, os.environ.get("APP_TIMEZONE"), results.day_inputs(start, end, day)],
                                     default=str).encode()).hexdigest()


def compute_day(day: str) -> dict:
    """Calculate one diary day from the local copy only."""
    profile_sync_error = sync_error = basal_sync_error = None
    settings_id, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    raw_entries, raw_treatments = mirror.cached_between(start, end)
    basal_doses = basal_store.basal_doses_between(start, end)
    entries = episodes.normalize_entries(raw_entries)
    treatments = episodes.normalize_treatments(raw_treatments)
    episodes.group_diary_followups(treatments, settings)
    meals = episodes.meals_for_day(treatments, day, settings["timezone"], settings)
    sport_marks = marks.analysis_sport_marks()
    manual_windows = marks.analysis_windows()
    exclusions = marks.analysis_exclusions()
    place_names = {place["id"]: place["name"] for place in place_store.all_places()}
    entry_places = place_store.entry_places()

    def place_of(item: dict) -> dict | None:
        place_id = entry_places.get(item.get("event_id") or "")
        return {"id": place_id, "name": place_names[place_id]} if place_id in place_names else None

    rendered = []
    main_meal_ids = {item["id"] for item in treatments
                     if (item.get("role") == "meal" or item.get("eventType") == "Meal Bolus")
                     and Decimal(str(item.get("carbs") or 0)) > 0}
    shown_in_meals = set()
    for meal in meals:
        meal_settings_id, meal_settings = config.get_settings_at(meal["at"])
        associated = [item for item in treatments if item.get("episode_id") == meal["id"] or item.get("id") == meal["source_id"]]
        output = {**meal}
        mark = sport_marks.get(meal["id"], {})
        confirmed_effect = calculation.dec(mark.get("effect_mgdl", 0)) if mark.get("status") == "affected" else Decimal(0)
        if not meal_settings["isf_schedule"]:
            output["calculation"] = {"reason": "Korrekturfaktor-Profil aus Nightscout fehlt"}
        else:
            try:
                output["calculation"] = calculation.calculate_episode(
                    meal, treatments, entries, meal_settings, confirmed_effect,
                    manual_windows.get(meal["id"]))
                output["calculation"]["settings_version"] = meal_settings_id
                output["calculation"]["nightscout_profile"] = meal_settings.get("nightscout_profile")
            except ValueError as error:
                output["calculation"] = {"reason": str(error)}
        if "auto_end" not in output["calculation"]:
            # Keep the window editable when the calculation itself is blocked.
            manual = manual_windows.get(meal["id"])
            automatic = calculation.automatic_window(meal, treatments, entries, meal_settings)
            output["calculation"].update({
                "auto_start": automatic["start"].isoformat(), "auto_end": automatic["end"].isoformat(),
                "manual_window": bool(manual),
                "window_start": (manual or automatic)["start"].isoformat(),
                "window_end": (manual or automatic)["end"].isoformat(),
            })
        # Sport is suggested when it ended within the lookback before the meal or falls into its window,
        # so a session during breakfast is offered for breakfast and, when it reaches back, for lunch too.
        window_end = parse_instant(output["calculation"].get("end") or output["calculation"]["window_end"])
        lookback = timedelta(minutes=int(meal_settings["sport_lookback_minutes"]))
        nearby_sport = [
            item for item in treatments
            if item.get("eventType") == "Exercise"
            and item["at"] + timedelta(minutes=int(item.get("duration") or 0)) >= meal["at"] - lookback
            and item["at"] < window_end
        ]
        proposed_effect = sum((
            calculation.dec(meal_settings["sport_effects_mgdl"].get(item.get("sport_type"), 0))
            for item in nearby_sport), Decimal(0))
        output["sport"] = {
            "status": mark.get("status", "auto"),
            "suggested": bool(nearby_sport),
            "suggested_effect_mgdl": str(proposed_effect),
            "effect_mgdl": str(confirmed_effect),
            "events": [{"id": item["id"], "at": item["at"], "kind": item.get("sport_type")}
                       for item in nearby_sport],
        }
        # A meal carries a data-quality mark when any of its counted carb entries does.
        carb_ids = ({item.get("id") for item in output["calculation"].get("carb_events", [])} | {meal["source_id"]}
                    | {item["id"] for item in associated if calculation.dec(item.get("carbs") or 0) > 0})
        output["quality"] = {
            "carbs_certainty": episodes.least_certain(item.get("carbs_certainty") for item in treatments if item["id"] in carb_ids),
            **{flag: any(item.get(flag) for item in treatments if item["id"] in carb_ids) for flag in episodes.QUALITY_MARKERS}}
        # Where the meal was eaten: its own entry's place, else that of its first counted carb entry with one.
        carb_entries = sorted((item for item in treatments if item["id"] in carb_ids),
                              key=lambda item: (item["id"] != meal["source_id"], item["at"]))
        output["place"] = next((place for place in map(place_of, carb_entries) if place), None)
        output["excluded"] = meal["id"] in exclusions
        output["review"] = meal_review(output)
        calculation_events = output["calculation"]
        included_ids = {item["id"] for key in ("carb_events", "doses")
                        for item in calculation_events.get(key, [])
                        if item.get("id") and item["id"] not in main_meal_ids}
        shown_in_meals.update(included_ids)
        event_ids = {item["id"] for item in associated}
        window_events = [item for item in treatments if item["id"] in included_ids and item["id"] not in event_ids]
        output["events"] = [episodes.serialize_treatment(item) for item in sorted(associated + window_events, key=lambda item: item["at"])]
        rendered.append(output)
    for output in rendered:
        output["window"] = meal_window(output, entries, settings)
    night = night_analysis(day, treatments, entries, rendered, settings)
    meal_sources = {meal["source_id"] for meal in meals}
    episode_ids = {meal["id"] for meal in meals if not meal["imported"]}
    zone = ZoneInfo(settings["timezone"])
    other_events = []
    exercises = []
    for item in treatments:
        if item["at"].astimezone(zone).date().isoformat() != day:
            continue
        if item["id"] in meal_sources or item["id"] in shown_in_meals or item.get("episode_id") in episode_ids:
            continue
        if episodes.BASAL_MARKER in item["notes"]:
            continue
        if item["eventType"] == "Exercise":
            kind = item.get("sport_type")
            note = episodes.EVENT_TAG.sub("", episodes.SPORT_TAG.sub("", item["notes"])).strip()
            for label in episodes.SPORT_LABELS.values():
                if note == label or note.startswith((label + " —", label + " |")):
                    note = note[len(label):].lstrip(" —|")
                    break
            exercises.append({
                "id": item["id"], "at": item["at"],
                "kind": kind, "label": episodes.SPORT_LABELS.get(kind, "Sportart unbekannt"),
                "duration_minutes": item["duration"], "notes": note,
            })
            continue
        other_events.append({
            "id": item["id"], "at": item["at"], "eventType": item["eventType"], "carbs": item["carbs"],
            "insulin": item["insulin"], "basal_units": item["basal_units"],
            "notes": episodes.QUALITY_TAG.sub("", episodes.EVENT_TAG.sub("", episodes.TAG.sub("", item["notes"]))).strip(),
        })
    timeline = []
    # Basal notes in Nightscout are shown from the local record, which also holds unsent doses.
    basal_items = [{"id": dose["id"], "at": dose["at"], "eventType": "Basal", "carbs": 0, "insulin": 0,
                    "basal_units": dose["units"], "duration": 0, "notes": dose["notes"],
                    "nightscout_synced": dose["nightscout_synced"]} for dose in basal_doses]
    for item in sorted([entry for entry in treatments if episodes.BASAL_MARKER not in entry["notes"]] + basal_items,
                       key=lambda entry: entry["at"]):
        if item["at"].astimezone(zone).date().isoformat() != day:
            continue
        event_type = item["eventType"]
        if event_type == "Basal":
            kind = "basal"
        elif event_type == "Exercise":
            kind = "exercise"
        elif item.get("finger_mgdl"):
            kind = "finger"
        elif event_type == "Meal Bolus" and calculation.dec(item["carbs"]) > 0:
            kind = "meal"
        elif event_type == "Snack Bolus" or "[DA2:snack]" in item["notes"]:
            kind = "snack"
        elif event_type == "Carb Correction":
            kind = "carbs"
        elif event_type in {"Correction Bolus", "Meal Bolus", "Bolus"} and calculation.dec(item["insulin"]) > 0:
            kind = "correction"
        else:
            kind = "other"
        note = episodes.QUALITY_TAG.sub("", episodes.EVENT_TAG.sub("", episodes.SPORT_TAG.sub("",
               episodes.TAG.sub("", item["notes"])))).replace("[DA2:snack]", "").strip()
        for label in episodes.SPORT_LABELS.values():
            if kind == "exercise" and (note == label or note.startswith(label + " — ")):
                note = note[len(label):].lstrip(" —")
                break
        try:
            if kind == "finger":
                glucose, glucose_at = item["finger_mgdl"], item["at"]
            else:
                glucose = calculation.interpolate_glucose(entries, item["at"])["value"]
                glucose_at = item["at"]
        except ValueError:
            previous = [point for point in entries
                        if point["at"] <= item["at"]
                        and item["at"] - point["at"] <= timedelta(minutes=15)]
            glucose = calculation.dec(previous[-1]["sgv"]) if previous else None
            glucose_at = previous[-1]["at"] if previous else None
            finger = None if previous else calculation.finger_glucose(treatments, item["at"])
            if finger:
                glucose, glucose_at = finger["value"], finger["before"]
        timeline.append({
            "id": item["id"], "at": item["at"], "kind": kind, "eventType": event_type,
            "carbs": item["carbs"], "insulin": item["insulin"],
            "basal_units": item["basal_units"], "duration_minutes": item["duration"],
            "sport_kind": item.get("sport_type"), "notes": note,
            "carbs_certainty": item.get("carbs_certainty"), "fat_protein": item.get("fat_protein", False),
            "alcohol": item.get("alcohol", False), "place": place_of(item),
            "glucose_mgdl": glucose, "glucose_at": glucose_at,
            **({"nightscout_synced": item["nightscout_synced"]} if kind == "basal" else {}),
        })
    for item in timeline:
        item["meal_id"] = block_meal_id(item, rendered)
    day_type, day_type_source = marks.day_type_for(day, marks.analysis_day_types(), marks.vacation_periods())
    return json_safe({
        "day": day, "connected": nightscout.is_configured(),
        "day_type": day_type, "day_type_source": day_type_source,
        "review": day_review(rendered),
        "synced_at": mirror.synced_at(day), "sync_error": sync_error,
        "profile_sync_error": profile_sync_error,
        "settings_version": settings_id, "meals": rendered, "exercises": exercises, "other_events": other_events,
        "timeline": timeline,
        "basal": [{"id": dose["id"], "at": dose["at"], "units": dose["units"], "notes": dose["notes"],
                   "nightscout_synced": dose["nightscout_synced"]} for dose in basal_doses],
        "basal_sync_error": basal_sync_error,
        "night": night,
        "entries": [{"at": item["at"], "sgv": item["sgv"], "tone": cgm.tone(item["sgv"], item["at"], settings)}
                    for item in entries],
        "target_bands": cgm.target_bands(start, end, settings),
        "treatments": [{**episodes.serialize_treatment(item), "rapid": calculation.is_rapid_insulin(item)}
                       for item in treatments],
        "window_limits": settings_service.MANUAL_WINDOW_LIMITS,
        "place_options": [{"id": place_id, "name": name} for place_id, name in place_names.items()],
    })


def meal_review(meal: dict) -> dict:
    """What is still open on a meal before its day counts as clean: a blocked factor or an
    undecided sport suggestion. Only a calculated factor can be excluded from the analysis."""
    calculated = meal["calculation"].get("factor_units_per_ke") is not None
    sport = meal["sport"]
    open_items = ([] if calculated else ["blocked"]) + (
        ["sport"] if sport["suggested"] and sport["status"] == "auto" else [])
    return {"open": open_items, "calculated": calculated, "excludable": calculated}


def day_review(meals: list[dict]) -> dict:
    """The day's check list: what still needs a look, and what is done."""
    open_meals = [meal for meal in meals if meal["review"]["open"]]
    return {
        "blocked": sum("blocked" in meal["review"]["open"] for meal in meals),
        "sport_open": sum("sport" in meal["review"]["open"] for meal in meals),
        "calculated": sum(meal["review"]["calculated"] and not meal["excluded"] for meal in meals),
        "excluded": sum(meal["review"]["calculated"] and meal["excluded"] for meal in meals),
        "first_open": open_meals[0]["id"] if open_meals else None,
    }


def _instant(value):
    return parse_instant(value) if isinstance(value, str) else value


def meal_window(meal: dict, entries: list[dict], settings: dict) -> dict:
    """The meal's counted window as the diary draws it, with the CGM value at both handles."""
    calc = meal["calculation"]
    fallback = meal["at"] + timedelta(minutes=int(settings.get("window_minutes", 180)))
    start = _instant(calc.get("start") or calc.get("window_start")) or meal["at"]
    end = _instant(calc.get("end") or calc.get("window_end")) or fallback

    def reading(at):
        try:
            value = calculation.interpolate_glucose(entries, at)["value"]
        except ValueError:
            return None
        return {"value": value, "tone": cgm.tone(value, at, settings)}

    return {"start": start, "end": end,
            "auto_start": _instant(calc.get("auto_start")) or meal["at"],
            "auto_end": _instant(calc.get("auto_end")) or fallback,
            "manual": bool(calc.get("manual_window")),
            "start_glucose": reading(start), "end_glucose": reading(end)}


def block_meal_id(item: dict, meals: list[dict]) -> str | None:
    """The diary groups an entry under its own main meal, else under the first meal whose window holds it."""
    own = next((meal for meal in meals if meal["source_id"] == item["id"]), None)
    if own is None:
        own = next((meal for meal in meals if meal["window"]["start"] <= item["at"] < meal["window"]["end"]), None)
    return own["id"] if own else None
