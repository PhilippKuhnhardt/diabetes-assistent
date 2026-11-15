"""Push reminders over Pushover (user decision 2026-10-09): a forgotten basal and a due second split part;
since 2026-10-11 also early warnings of a low or high within the next hour (app/glucose_alerts.py), and since
2026-11-14 a reminder to look at glucose and maybe correct after a meal (app/correction_check.py).

The background job checks them after each sync round; each reminder is sent once (its key is stored)
and only after a successful Nightscout sync, so a dose already given but not yet mirrored cannot
trigger a false reminder. A low or high warning repeats at most once per WARNING_REPEAT.
Each kind has its own Pushover priority (-2 to 2, user request 2026-10-11).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation, correction_check, forecast, glucose_alerts, on_board, split
from app.common import local_zone, parse_instant, unit_text
from app.ingestion import pushover
from app.services import day_view, dose_help, factors
from app.store import basal_doses, config, marks
from app.store import notifications as store

KINDS = {"basal": True, "split": True, "low": True, "high": True, "check": True}
LABELS = {"basal": "Basal vergessen", "split": "2. Split-Teil fällig", "low": "Tief droht", "high": "Hoch droht",
          "check": "BZ prüfen"}
# A second split part due before its planned time: glucose high and rising fast (app/split.py).
EARLY_SPLIT_TITLE = "2. Split-Teil jetzt: BZ steigt schnell"
WARNINGS = ("low", "high")
WARNING_REPEAT = timedelta(minutes=60)
# How soon each repeating kind may come again.
REPEAT = {"low": WARNING_REPEAT, "high": WARNING_REPEAT, "check": correction_check.REPEAT}
# Pushover priorities (https://pushover.net/api#priority); default 1 skips the quiet hours as before.
PRIORITY_LABELS = {-2: "Ganz leise (nur in der Pushover-App)", -1: "Leise (ohne Ton)", 0: "Normal",
                   1: "Hoch (auch in Ruhezeiten)", 2: "Notfall (wiederholt bis bestätigt)"}
PRIORITIES = {kind: pushover.PRIORITY for kind in KINDS}


def _units(value) -> str:
    return unit_text(calculation.dec(value)).replace(".", ",")


def _clock(at: datetime, zone: ZoneInfo) -> str:
    return at.astimezone(zone).strftime("%H:%M")


def due_reminders(now: datetime, settings: dict, treatments: list[dict], basal_today: list[dict],
                  enabled: dict[str, bool], entries: list[dict] | None = None,
                  second_part: Callable[[], Decimal | None] | None = None) -> list[dict]:
    """The reminders that are due now, before checking which were already sent. second_part gives the
    second split part's IE from the current forecast (None without a fresh glucose value)."""
    zone = local_zone(settings["timezone"])
    local = now.astimezone(zone)
    reminders = []
    breakfast_end = int(settings["breakfast_end_minute"])
    if enabled.get("basal") and local.hour * 60 + local.minute >= breakfast_end and not basal_today:
        usual = calculation.dec(settings.get("basal_default_units", config.DEFAULT_BASAL_UNITS))
        # Only the facts (user request 2026-10-11): Philipp knows the app, the title says what is due.
        message = f"Üblich {_units(usual)} IE" if usual > 0 else "Heute noch kein Basal"
        reminders.append({"key": f"basal:{local.date().isoformat()}", "kind": "basal",
                          "title": LABELS["basal"], "message": message})
    plan = split.open_plan(treatments, now) if enabled.get("split") else None
    state = split.timing(plan, entries or [], settings, now)["state"] if plan else None
    if state in ("early", "due"):
        # The amount comes from the current forecast (user approval 2026-10-22), the plan stays as orientation.
        units = second_part() if second_part else None
        meal = f"Mahlzeit um {_clock(plan['meal_at'], zone)}"
        if units is None:
            message = f"{_units(plan['units'])} IE zur {meal}"
        elif units > 0:
            message = f"{_units(units)} IE laut Prognose (geplant {_units(plan['units'])}) zur {meal}"
        else:
            message = f"Laut Prognose nicht nötig (geplant {_units(plan['units'])}) zur {meal}"
        # One reminder per plan: an early one is not repeated at the planned time.
        reminders.append({
            "key": f"split:{plan['meal_at'].astimezone(timezone.utc).isoformat()}", "kind": "split",
            "title": EARLY_SPLIT_TITLE if state == "early" else LABELS["split"], "message": message,
        })
    return reminders


def glucose_warnings(now: datetime, settings: dict, treatments: list[dict], entries: list[dict],
                     enabled: dict[str, bool]) -> list[dict]:
    """Low and high warnings due now (switched-on kinds only), before the once-per-hour check."""
    if not any(enabled.get(kind) for kind in WARNINGS):
        return []
    found = _acting(now, settings, treatments)
    if found is None:
        return []
    zone = local_zone(settings["timezone"])
    reminders = []
    for warning in glucose_alerts.warnings(entries, found, now, settings["timezone"]):
        if not enabled.get(warning["kind"]):
            continue
        minutes = max(0, round((warning["at"] - now).total_seconds() / 60))
        if warning["kind"] == "low":
            crossing, extreme = f"unter {glucose_alerts.LOW_MGDL}", "Tiefstwert"
        else:
            crossing, extreme = f"über {glucose_alerts.HIGH_MGDL}", "Höchstwert"
        message = (f"BZ {warning['glucose_mgdl']:.0f}, {crossing} um {_clock(warning['at'], zone)} "
                   f"(in {minutes} min), {extreme} {warning['mgdl']:.0f}")
        reminders.append({"key": f"{warning['kind']}:{now.astimezone(timezone.utc).isoformat()}",
                          "kind": warning["kind"], "title": LABELS[warning["kind"]], "message": message})
    return reminders


def _acting(now: datetime, settings: dict, treatments: list[dict]) -> dict | None:
    """What still acts now ("Wirkt noch"), as for the dose help; None when the model cannot be built."""
    zone = local_zone(settings["timezone"])
    today = now.astimezone(zone).date()
    overrides, periods = marks.analysis_day_types(), marks.vacation_periods()
    day_type = marks.day_type_for(today.isoformat(), overrides, periods)[0]
    _, _, lookback = factors.forecast_settings(settings)
    points = factors.meal_points(today, lookback, overrides, periods, mode=factors.vacation_mode(settings))
    method = settings.get("auto_method", "percentile")
    try:
        return on_board.sources(treatments, now, settings, factors.carb_factor(settings, today, points, day_type),
                                factors.surcharge_mgdl(settings) if method == forecast.SURCHARGE else None,
                                forecast.surcharge_share, sport_overrides=marks.sport_session_effects(),
                                fat_surcharge_mgdl=factors.fat_surcharge_mgdl(settings),
                                scale=factors.entry_scale(factors.place_scales(settings, today, overrides, periods)))
    except ValueError:
        return None


TEMPO_RISE = {"fast": "steil", "normal": "mittel", "slow": "flach"}
TEMPO_LEAN = {"fast": "wenig", "normal": "normal", "slow": "viel"}


def correction_reminder(now: datetime, settings: dict, treatments: list[dict], entries: list[dict],
                        enabled: dict[str, bool], correction: Callable[[], dict | None] | None = None,
                        found: dict | None = None) -> list[dict]:
    """The "BZ prüfen" reminder when due (app/correction_check.py), with the dose help's correction
    (user request 2026-11-14: name the suggested correction, Philipp judges the situation)."""
    if not enabled.get("check"):
        return []
    found = found if found is not None else _acting(now, settings, treatments)
    if found is None:
        return []
    hit = correction_check.due(entries, found, treatments, now)
    if hit is None:
        return []
    zone = local_zone(settings["timezone"])
    rise = hit["rise_mgdl"]
    message = (f"BZ {hit['glucose_mgdl']:.0f} ({'+' if rise >= 0 else ''}{rise:.0f} in 30 min), "
               f"Prognose Ende {hit['end_mgdl']:.0f}, Mahlzeit um {_clock(hit['meal_at'], zone)}")
    units = correction() if correction else None
    if units:
        normal, cautious = units.get("normal"), units.get("cautious")
        if normal is not None and normal > 0:
            message += f". Korrektur {_units(normal)} IE"
            if units.get("fast") is not None and units.get("slow") is not None:
                # The ends of what was right after recent meals; the meal's rise leans to one (user request 2026-11-15).
                message += f" (wenig {_units(units['fast'])} · viel {_units(units['slow'])} IE"
                tempo = units.get("tempo")
                if tempo:
                    message += f", Anstieg {TEMPO_RISE[tempo]}: eher {TEMPO_LEAN[tempo]}"
                message += ")"
            elif cautious is not None and cautious != normal:
                message += f" (vorsichtig {_units(cautious)} IE)"
        elif normal is not None:
            message += ". Laut Dosishilfe keine Korrektur nötig"
    return [{"key": f"check:{now.astimezone(timezone.utc).isoformat()}", "kind": "check",
             "title": LABELS["check"], "message": message}]


def _today_inputs(now: datetime, settings: dict) -> tuple[list[dict], list[dict], list[dict]]:
    zone = local_zone(settings["timezone"])
    today = now.astimezone(zone).date()
    # Today's day reaches back to yesterday afternoon, far enough for any open split plan.
    payload = day_view.day_payload(today.isoformat(), refresh=False)
    treatments = [{**row, "at": parse_instant(row["at"])} for row in payload["treatments"]]
    entries = [{"at": parse_instant(row["at"]), "sgv": row["sgv"]} for row in payload["entries"]]
    start = datetime.combine(today, time.min, tzinfo=zone)
    basal_today = basal_doses.basal_doses_between(start, start + timedelta(days=1))
    return treatments, basal_today, entries


def _second_part() -> Decimal | None:
    """The correction the dose help suggests now without food, which is the second split part's amount."""
    try:
        units = dose_help.dose_help({"carbs": "0"}).get("entry_units")
    except ValueError:
        return None
    return calculation.dec(units) if units is not None else None


def _correction() -> dict | None:
    """The dose help's correction now without food: normal and cautious IE, and after a recent meal the ends
    (fast = little need, slow = much) with the tempo the rise leans to (None without a fresh glucose value)."""
    try:
        result = dose_help.dose_help({"carbs": "0"})
    except ValueError:
        return None
    if result.get("entry_units") is None:
        return None
    scale = result.get("correction_scale")
    return {"normal": calculation.dec(result["entry_units"]),
            "cautious": calculation.dec(result["cautious_entry_units"]) if result.get("cautious_entry_units") is not None else None,
            "fast": calculation.dec(scale["fast_entry_units"]) if scale else None,
            "slow": calculation.dec(scale["slow_entry_units"]) if scale else None,
            "tempo": scale["tempo"] if scale else None}


def check(now: datetime | None = None) -> list[dict]:
    """Send every due reminder once. Returns what was sent; a failed send is retried next round."""
    if not pushover.is_configured():
        return []
    now = now or datetime.now(timezone.utc)
    enabled = store.enabled_kinds(KINDS)
    if not any(enabled.values()):
        return []
    _, settings = config.get_settings()
    treatments, basal_today, entries = _today_inputs(now, settings)
    priorities = store.priorities(PRIORITIES)
    sent = []
    for reminder in (due_reminders(now, settings, treatments, basal_today, enabled, entries, _second_part)
                     + glucose_warnings(now, settings, treatments, entries, enabled)
                     + correction_reminder(now, settings, treatments, entries, enabled, _correction)):
        if store.was_sent(reminder["key"]):
            continue
        last = store.last_sent(reminder["kind"]) if reminder["kind"] in REPEAT else None
        if last and now - last < REPEAT[reminder["kind"]]:
            continue
        pushover.send(reminder["title"], reminder["message"], priorities[reminder["kind"]])
        store.mark_sent(reminder["key"], reminder["kind"], reminder["title"], reminder["message"], now)
        sent.append(reminder)
    return sent


def view() -> dict:
    """Settings panel: whether Pushover is set up, which reminders are on, and the last ones sent."""
    _, settings = config.get_settings()
    minute = int(settings["breakfast_end_minute"])
    return {
        "configured": pushover.is_configured(),
        "enabled": store.enabled_kinds(KINDS),
        "priorities": store.priorities(PRIORITIES),
        "priority_options": [{"value": value, "label": label} for value, label in PRIORITY_LABELS.items()],
        "basal_after": f"{minute // 60:02d}:{minute % 60:02d}",
        "recent": [{**row, "label": LABELS.get(row["kind"], row["title"])} for row in store.recent()],
    }


def set_enabled(payload: dict) -> dict:
    """Switch a kind on or off ({kind, enabled}) or set its Pushover priority ({kind, priority})."""
    kind = payload.get("kind")
    if kind not in KINDS:
        raise ValueError("Ungültige Benachrichtigung")
    if "priority" in payload:
        priority = payload["priority"]
        if isinstance(priority, bool) or priority not in PRIORITY_LABELS:
            raise ValueError("Ungültige Stufe")
        store.set_priority(kind, priority, KINDS[kind])
        return view()
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("Ungültige Benachrichtigung")
    store.set_enabled(kind, enabled)
    return view()


def send_test(payload: dict) -> dict:
    try:
        pushover.send("Testmeldung", "Benachrichtigungen vom Diabetes-Assistenten kommen an.")
    except pushover.PushoverError as error:
        raise ValueError(str(error)) from None
    return {"message": "Testmeldung gesendet"}
