"""One diary day: mirror from Nightscout, calculate its meals, build the view, keep the result."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app import calculation, episodes, fpe, insulin, on_board
from app import glucose as cgm
from app.common import json_safe, local_day_range, local_zone, parse_instant, zone_name_at
from app.ingestion import nightscout, sync
from app.services import settings as settings_service
from app.store import basal_doses as basal_store, config, dishes as dish_store, marks, mirror, places as place_store, results
from app.analysis.night import night_analysis
from app import i18n
from app.i18n import tr

# Any change to the app's code or its language (APP_LANGUAGE: stored days hold texts) recalculates stored
# days once, so a stored result never outlives the code that produced it.
CODE_VERSION = hashlib.sha256(b"".join(
    path.read_bytes() for path in sorted(Path(__file__).resolve().parents[1].rglob("*.py"))) + i18n.LANGUAGE.encode()
    + insulin.ACTIVE.key.encode()).hexdigest()


def day_payload(day: str, refresh: bool = True) -> dict:
    """The diary day as the app shows it. With refresh it is first mirrored from Nightscout;
    the calculation is reused from SQLite while nothing it reads has changed."""
    memo = getattr(_pass, "memo", None)
    if refresh:
        status = sync.sync_day(day)
        if memo is not None:
            memo.clear()  # the sync may have changed the day, the profile and the basal doses
    else:
        status = {}
        if memo is not None and ("payload", day) in memo:
            # Within one_pass() a stored day is decoded once; callers there only read it.
            return dict(memo[("payload", day)])
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
    if memo is not None:
        memo[("payload", day)] = payload
        return dict(payload)
    return payload


# How far back "now" reads: longer than every rest that still acts (rapid insulin 6 h, carbs 12 h, a
# calculated surcharge 8 h, an open split part 3 h past due, a sport session's effect up to 24 h after its end).
RECENT_LOOKBACK = timedelta(hours=36)


def recent(now: datetime, refresh: bool = False) -> dict:
    """CGM readings and treatments from now − RECENT_LOOKBACK on, for everything computed "now" (dose help,
    "Wirkt noch", reminders and warnings). A calendar day would lose last evening's insulin, carbs, split plan
    and sport just after midnight, so this joins the stored days the span touches. Each treatment comes from
    its own local day's payload, where it was grouped with the rest of that day; the newest day's payload
    stands in for one it does not hold. With refresh today is first mirrored from Nightscout."""
    timezone_name = config.get_settings()[1]["timezone"]
    zone = local_zone(timezone_name)
    today = now.astimezone(zone).date()
    first = (now - RECENT_LOOKBACK).astimezone(zone).date()
    days = [(first + timedelta(days=offset)).isoformat() for offset in range((today - first).days + 1)]
    payloads = {day: day_payload(day, refresh=refresh and day == today.isoformat()) for day in days}
    since = now - RECENT_LOOKBACK
    entries, treatments = {}, {}
    for day in days:  # oldest first: a later day's copy of a reading or treatment replaces it ...
        for row in payloads[day].get("entries", []):
            if parse_instant(row["at"]) >= since:
                entries[row["at"]] = row
        for row in payloads[day].get("treatments", []):
            at = parse_instant(row["at"])
            if at < since:
                continue
            key = row.get("id") or (row["at"], row.get("eventType"), row.get("carbs"), row.get("insulin"))
            own_day = at.astimezone(zone).date().isoformat()
            if key not in treatments or day == own_day or treatments[key][0] != own_day:
                treatments[key] = (day, row)  # ... unless the copy came from the treatment's own day
    return {"entries": sorted(entries.values(), key=lambda row: parse_instant(row["at"])),
            "treatments": sorted((row for _, row in treatments.values()), key=lambda row: parse_instant(row["at"])),
            "today": payloads[today.isoformat()]}


def mirror_current(day: str) -> bool:
    """Whether a request can read the day from the local copy instead of asking Nightscout first:
    mirrored by this process after its last write to Nightscout and within one background round
    (plus a minute of slack; one minute without the job). Opening the app thus never waits on
    Nightscout while the background job keeps yesterday and today current."""
    from app.services import background
    synced = mirror.synced_at(day)
    if not synced or not sync.mirrored_after_writes(day):
        return False
    return datetime.now(timezone.utc) - parse_instant(synced) < timedelta(minutes=background.interval_minutes() + 1)


# Within one_pass(): each day's fingerprint and payload, and the settings and marks every day shares.
_pass = threading.local()


@contextmanager
def one_pass():
    """Read every day's inputs and stored payload at most once in this thread while the block runs.
    The background round asks for the same ~120 days several hundred times (stored days, five analysis
    pages, effects, reminders); nothing in the round changes them after its sync, and a sync inside
    the block starts the memo afresh. A result stored under a fingerprint that a concurrent write made
    stale is simply recalculated by the next reader, as before. Payloads handed out inside the block
    are shallow copies of one decoded payload, so their callers must not change nested values."""
    if getattr(_pass, "memo", None) is not None:
        yield
        return
    _pass.memo = {}
    try:
        yield
    finally:
        _pass.memo = None


def day_fingerprint(day: str) -> str:
    """Hash of everything compute_day reads, plus the code that computes it."""
    memo = getattr(_pass, "memo", None)
    if memo is None:
        timezone_name, shared = config.get_settings()[1]["timezone"], None
    else:
        if ("fingerprint", day) in memo:
            return memo[("fingerprint", day)]
        if "shared" not in memo:
            memo["shared"] = config.get_settings()[1]["timezone"], results.shared_inputs()
        timezone_name, shared = memo["shared"]
    start, end = local_day_range(day, timezone_name)
    value = hashlib.sha256(json.dumps([CODE_VERSION, os.environ.get("APP_TIMEZONE"),
                                       results.day_inputs(start, end, day, shared)],
                                      default=str).encode()).hexdigest()
    if memo is not None:
        memo[("fingerprint", day)] = value
    return value


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
    session_effects = marks.sport_session_effects()
    session_minutes = marks.sport_session_minutes()
    manual_windows = marks.analysis_windows()
    exclusions = marks.analysis_exclusions()
    place_names = {place["id"]: place["name"] for place in place_store.all_places()}
    entry_places = place_store.entry_places()

    def place_of(item: dict) -> dict | None:
        place_id = entry_places.get(item.get("event_id") or "")
        return {"id": place_id, "name": place_names[place_id]} if place_id in place_names else None

    dish_names = {dish["id"]: dish["name"] for dish in dish_store.all_dishes()}
    meal_records = dish_store.records()

    def dish_of(item: dict) -> dict | None:
        dish_id = (meal_records.get(item.get("event_id") or "") or {}).get("dish_id")
        return {"id": dish_id, "name": dish_names[dish_id]} if dish_id in dish_names else None

    rendered = []
    main_meal_ids = {item["id"] for item in treatments
                     if (item.get("role") == "meal" or item.get("eventType") == "Meal Bolus")
                     and Decimal(str(item.get("carbs") or 0)) > 0}
    shown_in_meals = set()
    for meal in meals:
        meal_settings_id, meal_settings = config.get_settings_at(meal["at"])
        associated = [item for item in treatments if item.get("episode_id") == meal["id"] or item.get("id") == meal["source_id"]]
        output = {**meal}
        # Sport counts with the share of each session's effect that acts inside the meal's window (the same
        # curve as "Wirkt noch"). Its effect is set on the session only (user decision 2026-11-23: a value per
        # meal could contradict the session), so the former per-meal marks are no longer read.
        try:
            span = manual_windows.get(meal["id"]) or calculation.automatic_window(meal, treatments, entries, meal_settings)
        except (ValueError, KeyError):
            span = {"start": meal["at"], "end": meal["at"] + timedelta(minutes=int(meal_settings["window_minutes"]))}
        sport_parts = [
            (item, on_board.sport_effect(item, meal_settings, session_effects)
             * on_board.sport_share(item, span["start"], span["end"], on_board.sport_minutes(item, meal_settings, session_minutes)))
            for item in treatments
            if item.get("eventType") == "Exercise" and item.get("sport_type") and item["at"] < span["end"]]
        sport_parts = [(item, effect) for item, effect in sport_parts if effect != 0]
        auto_effect = sum((effect for _, effect in sport_parts), Decimal(0))
        confirmed_effect = auto_effect
        if not meal_settings["isf_schedule"]:
            output["calculation"] = {"reason": tr("Korrekturfaktor-Profil aus Nightscout fehlt")}
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
        output["sport"] = {
            "status": "auto",
            "suggested": bool(sport_parts),
            "suggested_effect_mgdl": str(auto_effect),
            "effect_mgdl": str(confirmed_effect),
            "events": [{"id": item["id"], "at": item["at"], "kind": item.get("sport_type"), "effect_mgdl": str(effect),
                        "previous_day": item["at"].astimezone(local_zone(settings["timezone"])).date().isoformat() < day,
                        # When the session's effect acts (its end until its effect duration ran out), for the curve's band.
                        **_effect_span(item, meal_settings, session_minutes)}
                       for item, effect in sport_parts],
        }
        # A meal carries a data-quality mark when any of its counted carb entries does.
        carb_ids = ({item.get("id") for item in output["calculation"].get("carb_events", [])} | {meal["source_id"]}
                    | {item["id"] for item in associated if calculation.dec(item.get("carbs") or 0) > 0})
        output["quality"] = {
            "carbs_certainty": episodes.least_certain(item.get("carbs_certainty") for item in treatments if item["id"] in carb_ids),
            **{flag: any(item.get(flag) for item in treatments if item["id"] in carb_ids) for flag in episodes.QUALITY_MARKERS}}
        # The calculated fat/protein surcharge its own entry was dosed with (app/fpe.py), else None (static).
        output["fpe_surcharge"] = next((item.get("surcharge") for item in treatments if item["id"] == meal["source_id"]), None)
        # Where the meal was eaten: its own entry's place, else that of its first counted carb entry with one.
        carb_entries = sorted((item for item in treatments if item["id"] in carb_ids),
                              key=lambda item: (item["id"] != meal["source_id"], item["at"]))
        output["place"] = next((place for place in map(place_of, carb_entries) if place), None)
        # What the meal was: the dish of its own entry, else of its first counted carb entry with one.
        output["dish"] = next((dish for dish in map(dish_of, carb_entries) if dish), None)
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
    zone = local_zone(settings["timezone"])
    # Sport kind names come from the current settings, not the day's version: a kind added or renamed today
    # names older sessions too (every settings save is part of the day fingerprint).
    current_settings = config.get_settings()[1]
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
            for label in episodes.note_labels(current_settings):
                if note == label or note.startswith((label + " —", label + " |")):
                    note = note[len(label):].lstrip(" —|")
                    break
            exercises.append({
                "id": item["id"], "at": item["at"],
                "kind": kind, "label": episodes.sport_names(current_settings).get(kind, tr("Sportart unbekannt")),
                "duration_minutes": item["duration"], "notes": note,
                **sport_session(item, settings, session_effects, session_minutes),
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
        for label in episodes.note_labels(current_settings):
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
            **(sport_session(item, settings, session_effects, session_minutes) if kind == "exercise" else {}),
            "carbs_certainty": item.get("carbs_certainty"), "fat_protein": item.get("fat_protein", False),
            "alcohol": item.get("alcohol", False), "fast": item.get("fast", False), "place": place_of(item),
            "dish": dish_of(item), "event_id": item.get("event_id"), "surcharge": item.get("surcharge"),
            **({"fpe_level": fpe.level_of(item.get("surcharge"), item.get("fat_protein", False))}
               if kind in ("meal", "snack", "carbs") and settings.get("auto_method") == "surcharge" else {}),
            "description": (meal_records.get(item.get("event_id") or "") or {}).get("description") or "",
            "glucose_mgdl": glucose, "glucose_at": glucose_at,
            **({"nightscout_synced": item["nightscout_synced"]} if kind == "basal" else {}),
        })
    for item in timeline:
        item["meal_id"] = block_meal_id(item, rendered)
        if item["kind"] == "exercise":
            item.update(sport_shares(item, rendered))
    periods = marks.vacation_periods()
    day_type, day_type_source = marks.day_type_for(day, marks.analysis_day_types(), periods)
    # Vacation mark: every meal of a vacation day carries it, independent
    # of a manual day type; the Automatik skips these days after the vacation (factors.counted_days).
    vacation = marks.in_vacation(day, periods)
    for meal in rendered:
        meal["vacation"] = vacation
    # Local midnight to midnight, for the day charts: travel days are shorter or longer (TravelZone).
    local = local_zone(settings["timezone"])
    parsed = date.fromisoformat(day)
    day_bounds = [datetime.combine(parsed + timedelta(days=offset), datetime.min.time(), tzinfo=local)
                  .astimezone(timezone.utc) for offset in (0, 1)]
    return json_safe({
        "day": day, "connected": nightscout.is_configured(),
        "day_start": day_bounds[0], "day_end": day_bounds[1],
        "timezone": zone_name_at(settings["timezone"], day_bounds[0] + (day_bounds[1] - day_bounds[0]) / 2),
        "day_type": day_type, "day_type_source": day_type_source, "vacation": vacation,
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
        "dish_options": [{"id": dish_id, "name": name} for dish_id, name in dish_names.items()],
    })


def sport_session(item: dict, settings: dict, session_effects: dict, session_minutes: dict) -> dict:
    """A session's glucose drop and effect duration as they count, and whether the user set them themselves."""
    return {"sport_effect_mgdl": str(on_board.sport_effect(item, settings, session_effects)),
            "sport_effect_default_mgdl": str(on_board.sport_effect(item, settings)),
            "sport_effect_custom": item["id"] in session_effects,
            "sport_minutes": int(on_board.sport_minutes(item, settings, session_minutes)),
            "sport_minutes_default": int(on_board.sport_minutes(item, settings)),
            "sport_minutes_custom": item["id"] in session_minutes}


def _effect_span(item: dict, settings: dict, session_minutes: dict) -> dict:
    end = item["at"] + timedelta(minutes=float(item.get("duration") or 0))
    return {"effect_from": end, "effect_until": end + timedelta(minutes=float(on_board.sport_minutes(item, settings, session_minutes)))}


def sport_shares(item: dict, meals: list[dict]) -> dict:
    """Where a session's effect lands (Verlauf's sport card, the entry card): each meal of the day gets the
    part acting inside its window; the rest acts between or after the meals (also next morning)."""
    shares = [{"meal_id": meal["id"], "meal": meal["meal"], "effect_mgdl": event["effect_mgdl"]}
              for meal in meals for event in meal["sport"]["events"] if event["id"] == item["id"]]
    total = calculation.dec(item["sport_effect_mgdl"])
    rest = total - sum((calculation.dec(share["effect_mgdl"]) for share in shares), Decimal(0))
    until = item["at"] + timedelta(minutes=float(item["duration_minutes"] or 0) + item["sport_minutes"])
    return {"sport_shares": shares, "sport_rest_mgdl": str(rest.quantize(Decimal("0.1"))),
            "sport_effect_until": until if total else None}


# Entries are reviewed from this day on;
# the red dot on "Verlauf" counts finished days from here up to yesterday.
REVIEW_SINCE = "2026-11-24"


def with_checks(payload: dict, now: datetime | None = None) -> dict:
    """The day with the user's checks laid over it at request time, so a check never recalculates a stored
    day: "Passt" on a meal holds only for the window it was given for, and only
    a finished meal can be checked. Only main meals need a check: reviewing is about the factor, so snacks, sport and other entries outside the meal cards never do. The day is done once
    every meal is checked or excluded."""
    now = now or datetime.now(timezone.utc)
    checks = marks.window_checks()
    meals = []
    for meal in payload.get("meals", []):
        window = meal.get("window") or {}
        stored = checks.get(meal["id"])
        checked = bool(stored and window.get("start") and window.get("end")
                       and parse_instant(stored[0]) == _instant(window["start"])
                       and parse_instant(stored[1]) == _instant(window["end"]))
        finished = bool(window.get("end")) and _instant(window["end"]) <= now
        meals.append({**meal, "review": {**meal["review"], "checked": checked, "checkable": finished}})
    open_meals = sum(not meal["review"]["checked"] and not meal.get("excluded") for meal in meals)
    review = {**payload.get("review", {}),
              "checked": sum(meal["review"]["checked"] for meal in meals),
              "tasks": sum(bool(meal["review"]["open"]) and not meal["review"]["checked"] and not meal.get("excluded")
                           for meal in meals),
              "total": len(meals),
              # What still waits for a look, and whether "Tag prüfen" can check it all (every meal finished).
              "unchecked": open_meals,
              "checkable": all(meal["review"]["checkable"] for meal in meals),
              "done": bool(meals) and open_meals == 0}
    return {**payload, "meals": meals, "review": review}


def week_view(params: dict) -> dict:
    """Verlauf's week strip: Monday to Sunday of the week holding `date`, each day
    with its day type, open tasks and whether everything on it is checked. Days after today are only
    named; they are not calculated."""
    day = date.fromisoformat((params.get("date") or [""])[0])
    _, settings = config.get_settings()
    today = datetime.now(timezone.utc).astimezone(local_zone(settings["timezone"])).date()
    monday = day - timedelta(days=day.weekday())
    days = []
    for offset in range(7):
        current = monday + timedelta(days=offset)
        row = {"day": current.isoformat(), "future": current > today}
        if not row["future"]:
            review = _checked_day(current.isoformat())
            row.update({"day_type": review["day_type"], "meals": review["meals"],
                        "checked": review["checked"], "tasks": review["tasks"], "done": review["done"]})
        days.append(row)
    return {"date": day.isoformat(), "today": today.isoformat(), "week": int(monday.isocalendar()[1]), "days": days}


def _checked_day(day: str) -> dict:
    # A day never mirrored yet is read from Nightscout once, so its dot is right without opening it.
    payload = with_checks(day_payload(day, refresh=not mirror.synced_at(day)))
    return {**payload["review"], "day_type": payload["day_type"], "meals": len(payload["meals"])}


def pending_review(params: dict | None = None) -> dict:
    """The red dot on "Verlauf": finished days (before today) since REVIEW_SINCE
    with something not yet checked, oldest first, so a tap can open the first of them."""
    _, settings = config.get_settings()
    today = datetime.now(timezone.utc).astimezone(local_zone(settings["timezone"])).date()
    current, days = date.fromisoformat(REVIEW_SINCE), []
    while current < today:
        if _checked_day(current.isoformat())["unchecked"]:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return {"since": REVIEW_SINCE, "days": days}


def meal_review(meal: dict) -> dict:
    """What is still open on a meal before its day counts as clean: a blocked factor (sport now counts
    automatically). Only a calculated factor can be excluded from the analysis."""
    calculated = meal["calculation"].get("factor_units_per_ke") is not None
    open_items = [] if calculated else ["blocked"]
    return {"open": open_items, "calculated": calculated, "excludable": calculated}


def day_review(meals: list[dict]) -> dict:
    """The day's check list: what still needs a look, and what is done."""
    open_meals = [meal for meal in meals if meal["review"]["open"]]
    return {
        "blocked": sum("blocked" in meal["review"]["open"] for meal in meals),
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
