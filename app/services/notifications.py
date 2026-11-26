"""Push reminders over Pushover: a forgotten basal and a due second split part;
since 2026-10-11 also early warnings of a low or high within the next hour (app/glucose_alerts.py), and since
2026-11-14 a reminder to look at glucose and maybe correct after a meal (app/correction_check.py).

The background job checks them after each sync round; each reminder is sent once (its key is stored)
and only after a successful Nightscout sync, so a dose already given but not yet mirrored cannot
trigger a false reminder. A low or high warning repeats at most once per WARNING_REPEAT.
Each kind has its own Pushover priority.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation, correction_check, forecast, glucose_alerts, on_board, split
from app import glucose as cgm
from app.common import local_zone, parse_instant, unit_text
from app.ingestion import pushover
from app.services import day_view, dose_help, factors
from app.store import basal_doses, config, marks
from app.store import notifications as store
from app.i18n import english, tr

KINDS = {"basal": True, "split": True, "low": True, "high": True, "check": True}
LABELS = {"basal": tr("Basal vergessen"), "split": tr("2. Split-Teil fällig"), "low": tr("Tief droht"), "high": tr("Hoch droht"),
          "check": tr("BZ prüfen")}
# A second split part due before its planned time: glucose high and rising fast (app/split.py).
EARLY_SPLIT_TITLE = tr("2. Split-Teil jetzt: BZ steigt schnell")
WARNINGS = ("low", "high")
WARNING_REPEAT = timedelta(minutes=60)
# How soon each repeating kind may come again.
REPEAT = {"low": WARNING_REPEAT, "high": WARNING_REPEAT, "check": correction_check.REPEAT}
# Pushover priorities (https://pushover.net/api#priority); default 1 skips the quiet hours as before.
PRIORITY_LABELS = {-2: tr("Ganz leise (nur in der Pushover-App)"), -1: tr("Leise (ohne Ton)"), 0: tr("Normal"),
                   1: tr("Hoch (auch in Ruhezeiten)"), 2: tr("Notfall (wiederholt bis bestätigt)")}
PRIORITIES = {kind: pushover.PRIORITY for kind in KINDS}
# Minutes without a warning: low after a carb entry, high after rapid insulin.
PAUSES = {"low": glucose_alerts.LOW_PAUSE_MINUTES, "high": glucose_alerts.HIGH_PAUSE_MINUTES}
MAX_PAUSE_MINUTES = 180


def _units(value) -> str:
    text = unit_text(calculation.dec(value))
    return text if english() else text.replace(".", ",")


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
        # Only the facts: The user knows the app, the title says what is due.
        message = tr("Üblich {units} IE", units=_units(usual)) if usual > 0 else tr("Heute noch kein Basal")
        reminders.append({"key": f"basal:{local.date().isoformat()}", "kind": "basal",
                          "title": LABELS["basal"], "message": message})
    plan = split.open_plan(treatments, now) if enabled.get("split") else None
    state = split.timing(plan, entries or [], settings, now)["state"] if plan else None
    if state in ("early", "due"):
        # The amount comes from the current forecast, the plan stays as orientation.
        units = second_part() if second_part else None
        meal = tr("Mahlzeit um {time}", time=_clock(plan["meal_at"], zone))
        if units is None:
            message = tr("{units} IE zur {meal}", units=_units(plan["units"]), meal=meal)
        elif units > 0:
            message = tr("{units} IE laut Prognose (geplant {planned}) zur {meal}", units=_units(units), planned=_units(plan["units"]), meal=meal)
        else:
            message = tr("Laut Prognose nicht nötig (geplant {planned}) zur {meal}", planned=_units(plan["units"]), meal=meal)
        # One reminder per plan: an early one is not repeated at the planned time.
        reminders.append({
            "key": f"split:{plan['meal_at'].astimezone(timezone.utc).isoformat()}", "kind": "split",
            "title": EARLY_SPLIT_TITLE if state == "early" else LABELS["split"], "message": message,
        })
    return reminders


def glucose_warnings(now: datetime, settings: dict, treatments: list[dict], entries: list[dict],
                     enabled: dict[str, bool], pauses: dict[str, int] | None = None) -> list[dict]:
    """Low and high warnings due now (switched-on kinds only), before the once-per-hour check."""
    pauses = {**PAUSES, **(pauses or {})}
    if not any(enabled.get(kind) for kind in WARNINGS):
        return []
    found = _acting(now, settings, treatments)
    if found is None:
        return []
    zone = local_zone(settings["timezone"])
    reminders = []
    low_limit, high_limit = cgm.limits(settings)
    for warning in glucose_alerts.warnings(entries, found, now, settings["timezone"],
                                           low_pause_minutes=pauses["low"], high_pause_minutes=pauses["high"],
                                           low_mgdl=low_limit, high_mgdl=high_limit):
        if not enabled.get(warning["kind"]):
            continue
        minutes = max(0, round((warning["at"] - now).total_seconds() / 60))
        if warning["kind"] == "low":
            crossing, extreme = tr("unter {mgdl}", mgdl=cgm.shown(low_limit, settings)), tr("Tiefstwert")
        else:
            crossing, extreme = tr("über {mgdl}", mgdl=cgm.shown(high_limit, settings)), tr("Höchstwert")
        message = tr("BZ {glucose}, {crossing} um {time} (in {minutes} min), {extreme} {mgdl}",
                     glucose=cgm.shown(warning['glucose_mgdl'], settings), crossing=crossing, time=_clock(warning["at"], zone),
                     minutes=minutes, extreme=extreme, mgdl=cgm.shown(warning['mgdl'], settings))
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
            sport_minutes_overrides=marks.sport_session_minutes(),
                                fat_surcharge_mgdl=factors.fat_surcharge_mgdl(settings),
                                scale=factors.entry_scale(factors.place_scales(settings, today, overrides, periods)))
    except ValueError:
        return None


TEMPO_LEAN = {"fast": tr("wenig"), "normal": tr("normal"), "slow": tr("viel")}


def correction_reminder(now: datetime, settings: dict, treatments: list[dict], entries: list[dict],
                        enabled: dict[str, bool], correction: Callable[[], dict | None] | None = None,
                        found: dict | None = None) -> list[dict]:
    """The tr("BZ prüfen") reminder when due (app/correction_check.py), with the dose help's correction."""
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
    message = tr("BZ {glucose} ({rise} in 30 min), Prognose Ende {end}, Mahlzeit um {time}",
                 glucose=cgm.shown(hit['glucose_mgdl'], settings), rise=cgm.shown(rise, settings, signed=True),
                 end=cgm.shown(hit['end_mgdl'], settings), time=_clock(hit["meal_at"], zone))
    units = correction() if correction else None
    if units:
        normal, cautious = units.get("normal"), units.get("cautious")
        if normal is not None and normal > 0:
            message += tr(". Korrektur {units} IE", units=_units(normal))
            if units.get("fast") is not None and units.get("slow") is not None:
                # The ends of what was right after recent meals; the meal's rise leans to one.
                message += tr(" (wenig {fast} · viel {slow} IE", fast=_units(units["fast"]), slow=_units(units["slow"]))
                tempo = units.get("tempo")
                if tempo:
                    message += tr(", eher {lean}", lean=TEMPO_LEAN[tempo])
                message += ")"
            elif cautious is not None and cautious != normal:
                message += tr(" (vorsichtig {units} IE)", units=_units(cautious))
        elif normal is not None:
            message += tr(". Laut Dosishilfe keine Korrektur nötig")
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
                     + glucose_warnings(now, settings, treatments, entries, enabled, store.pauses(PAUSES))
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
        "pauses": store.pauses(PAUSES),
        "priority_options": [{"value": value, "label": label} for value, label in PRIORITY_LABELS.items()],
        "basal_after": f"{minute // 60:02d}:{minute % 60:02d}",
        "recent": [{**row, "label": LABELS.get(row["kind"], row["title"])} for row in store.recent()],
    }


def set_enabled(payload: dict) -> dict:
    """Switch a kind on or off ({kind, enabled}), set its Pushover priority ({kind, priority}) or, for low and
    high, its pause after carbs or insulin ({kind, pause_minutes})."""
    kind = payload.get("kind")
    if kind not in KINDS:
        raise ValueError(tr("Ungültige Benachrichtigung"))
    if "pause_minutes" in payload:
        minutes = payload["pause_minutes"]
        if (kind not in PAUSES or isinstance(minutes, bool) or not isinstance(minutes, int)
                or not 0 <= minutes <= MAX_PAUSE_MINUTES):
            raise ValueError(tr("Pause muss zwischen 0 und {minutes} Minuten liegen", minutes=MAX_PAUSE_MINUTES))
        store.set_pause(kind, minutes, KINDS[kind])
        return view()
    if "priority" in payload:
        priority = payload["priority"]
        if isinstance(priority, bool) or priority not in PRIORITY_LABELS:
            raise ValueError(tr("Ungültige Stufe"))
        store.set_priority(kind, priority, KINDS[kind])
        return view()
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError(tr("Ungültige Benachrichtigung"))
    store.set_enabled(kind, enabled)
    return view()


def send_test(payload: dict) -> dict:
    try:
        pushover.send(tr("Testmeldung"), tr("Benachrichtigungen vom Diabetes-Assistenten kommen an."))
    except pushover.PushoverError as error:
        raise ValueError(str(error)) from None
    return {"message": tr("Testmeldung gesendet")}
