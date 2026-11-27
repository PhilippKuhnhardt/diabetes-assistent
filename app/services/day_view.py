"""One diary day: mirror from Nightscout, hand its snapshot to the Rust core, keep the result."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import core, diabetes_core, episodes, insulin
from app.common import json_safe, local_day_range, local_zone, parse_instant
from app.ingestion import nightscout, sync
from app.store.db import connect
from app.store import basal_doses as basal_store, config, dishes as dish_store, marks, mirror, places as place_store, results
from app import i18n

# Any change to the app's code or its language (APP_LANGUAGE: stored days hold texts) recalculates stored
# days once, so a stored result never outlives the code that produced it.
CODE_VERSION = hashlib.sha256(b"".join(
    path.read_bytes() for path in sorted(Path(__file__).resolve().parents[1].rglob("*.py")))
    + Path(diabetes_core.__file__).read_bytes() + i18n.LANGUAGE.encode()
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


def day_snapshot(day: str) -> dict:
    """Everything compute_day reads from the store, as JSON for the Rust core (core/diabetes-core/src/day.rs):
    the day's raw Nightscout rows and basal doses, the settings (current and the versions in force during the
    day), the marks and the names of places and dishes its entries carry."""
    settings_id, settings = config.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    raw_entries, raw_treatments = mirror.cached_between(start, end)
    event_ids = {episodes.event_id(item) for item in raw_treatments} - {None}
    with connect() as db:
        rows = db.execute("SELECT id, effective_at, payload FROM settings_versions ORDER BY effective_at, id").fetchall()
    # get_settings_at(moment): the newest version in force at the moment, else the current one. Versions
    # that took effect before the day's range only matter through the last of them.
    versions, before = [], None
    for row in rows:
        effective = parse_instant(row["effective_at"])
        if effective <= start:
            before = row
        elif effective < end:
            versions.append(row)
    versions = ([before] if before is not None else []) + versions
    meal_records = dish_store.records()
    entry_places = place_store.entry_places()
    return json_safe({
        "day": day, "connected": nightscout.is_configured(), "synced_at": mirror.synced_at(day),
        "settings_id": settings_id, "settings": settings,
        "settings_versions": [{"id": row["id"], "effective_at": parse_instant(row["effective_at"]),
                               "settings": config._with_nightscout_profile(json.loads(row["payload"]))}
                              for row in versions],
        "raw_entries": raw_entries, "raw_treatments": raw_treatments,
        "basal_doses": basal_store.basal_doses_between(start, end),
        "sport_session_effects": marks.sport_session_effects(), "sport_session_minutes": marks.sport_session_minutes(),
        "analysis_windows": marks.analysis_windows(), "analysis_exclusions": sorted(marks.analysis_exclusions()),
        "day_types": marks.analysis_day_types(), "vacation_periods": marks.vacation_periods(),
        "places": [{"id": place["id"], "name": place["name"]} for place in place_store.all_places()],
        "entry_places": {key: value for key, value in entry_places.items() if key in event_ids},
        "dishes": [{"id": dish["id"], "name": dish["name"]} for dish in dish_store.all_dishes()],
        "meal_records": {key: {"dish_id": value.get("dish_id"), "description": value.get("description") or ""}
                         for key, value in meal_records.items() if key in event_ids},
    })


def compute_day(day: str) -> dict:
    """Calculate one diary day from the local copy only (the rules: core/diabetes-core/src/day.rs)."""
    return core.call("day.compute_day", **day_snapshot(day))


# Entries are reviewed from this day on;
# the red dot on "Verlauf" counts finished days from here up to yesterday.
REVIEW_SINCE = "2026-11-24"


def with_checks(payload: dict, now: datetime | None = None) -> dict:
    """The day with the user's checks laid over it at request time, so a check never recalculates a stored
    day: "Passt" on a meal holds only for the window it was given for, and only
    a finished meal can be checked. Only main meals need a check: reviewing is about the factor, so snacks, sport and other entries outside the meal cards never do. The day is done once
    every meal is checked or excluded. (The rule: day.rs with_checks.)"""
    meals = payload.get("meals", [])
    result = core.call("day.with_checks", now=now or datetime.now(timezone.utc), checks=marks.window_checks(),
                       meals=[{key: meal.get(key) for key in ("id", "window", "review", "excluded")} for meal in meals])
    return {**payload, "meals": [{**meal, "review": review} for meal, review in zip(meals, result["meals"])],
            "review": {**payload.get("review", {}), **result["review"]}}


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
    """What is still open on a meal before its day counts as clean (the rule: day.rs meal_review)."""
    return core.call("day.meal_review", meal=meal)


def day_review(meals: list[dict]) -> dict:
    """The day's check list: what still needs a look, and what is done (day.rs day_review)."""
    return core.call("day.day_review", meals=meals)
