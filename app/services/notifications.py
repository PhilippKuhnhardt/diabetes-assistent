"""Push reminders over Pushover (user decision 2026-10-09): a forgotten basal and a due second split part;
since 2026-10-11 also early warnings of a low or high within the next hour (app/glucose_alerts.py).

The background job checks them after each sync round; each reminder is sent once (its key is stored)
and only after a successful Nightscout sync, so a dose already given but not yet mirrored cannot
trigger a false reminder. A low or high warning repeats at most once per WARNING_REPEAT.
Each kind has its own Pushover priority (-2 to 2, user request 2026-10-11).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import calculation, forecast, glucose_alerts, on_board, split
from app.common import parse_instant, unit_text
from app.ingestion import pushover
from app.services import day_view, factors
from app.store import basal_doses, config, marks
from app.store import notifications as store

KINDS = {"basal": True, "split": True, "low": True, "high": True}
LABELS = {"basal": "Basal vergessen", "split": "2. Split-Teil fällig", "low": "Tief droht", "high": "Hoch droht"}
WARNINGS = ("low", "high")
WARNING_REPEAT = timedelta(minutes=60)
# Pushover priorities (https://pushover.net/api#priority); default 1 skips the quiet hours as before.
PRIORITY_LABELS = {-2: "Ganz leise (nur in der Pushover-App)", -1: "Leise (ohne Ton)", 0: "Normal",
                   1: "Hoch (auch in Ruhezeiten)", 2: "Notfall (wiederholt bis bestätigt)"}
PRIORITIES = {kind: pushover.PRIORITY for kind in KINDS}


def _units(value) -> str:
    return unit_text(calculation.dec(value)).replace(".", ",")


def _clock(at: datetime, zone: ZoneInfo) -> str:
    return at.astimezone(zone).strftime("%H:%M")


def due_reminders(now: datetime, settings: dict, treatments: list[dict], basal_today: list[dict],
                  enabled: dict[str, bool]) -> list[dict]:
    """The reminders that are due now, before checking which were already sent."""
    zone = ZoneInfo(settings["timezone"])
    local = now.astimezone(zone)
    reminders = []
    breakfast_end = int(settings["breakfast_end_minute"])
    if enabled.get("basal") and local.hour * 60 + local.minute >= breakfast_end and not basal_today:
        usual = calculation.dec(settings.get("basal_default_units", config.DEFAULT_BASAL_UNITS))
        message = f"Bis {breakfast_end // 60:02d}:{breakfast_end % 60:02d} ist heute noch kein Basal eingetragen."
        if usual > 0:
            message += f" Üblich sind {_units(usual)} IE."
        reminders.append({"key": f"basal:{local.date().isoformat()}", "kind": "basal",
                          "title": LABELS["basal"], "message": message})
    plan = split.open_plan(treatments, now) if enabled.get("split") else None
    if plan and now >= plan["due_at"]:
        reminders.append({
            "key": f"split:{plan['meal_at'].astimezone(timezone.utc).isoformat()}", "kind": "split",
            "title": LABELS["split"],
            "message": (f"Geplant sind {_units(plan['units'])} IE zur Mahlzeit um {_clock(plan['meal_at'], zone)}. "
                        "Die App rechnet den 2. Teil mit dem aktuellen BZ nach."),
        })
    return reminders


def glucose_warnings(now: datetime, settings: dict, treatments: list[dict], entries: list[dict],
                     enabled: dict[str, bool]) -> list[dict]:
    """Low and high warnings due now (switched-on kinds only), before the once-per-hour check."""
    if not any(enabled.get(kind) for kind in WARNINGS):
        return []
    zone = ZoneInfo(settings["timezone"])
    today = now.astimezone(zone).date()
    overrides, periods = marks.analysis_day_types(), marks.vacation_periods()
    day_type = marks.day_type_for(today.isoformat(), overrides, periods)[0]
    _, _, lookback = factors.forecast_settings(settings)
    points = factors.meal_points(today, lookback, overrides, periods)
    method = settings.get("auto_method", "percentile")
    try:
        found = on_board.sources(treatments, now, settings, factors.carb_factor(settings, today, points, day_type),
                                 factors.surcharge_mgdl(settings) if method == forecast.SURCHARGE else None,
                                 forecast.surcharge_share)
    except ValueError:
        return []
    reminders = []
    for warning in glucose_alerts.warnings(entries, found, now, settings["timezone"]):
        if not enabled.get(warning["kind"]):
            continue
        minutes = max(0, round((warning["at"] - now).total_seconds() / 60))
        glucose = f"BZ {warning['glucose_mgdl']:.0f} mg/dL"
        if warning["kind"] == "low":
            message = (f"{glucose}. Prognose unter {glucose_alerts.LOW_MGDL} um {_clock(warning['at'], zone)} "
                       f"(in etwa {minutes} min), tiefster Wert in der nächsten Stunde etwa {warning['mgdl']:.0f}.")
        else:
            message = (f"{glucose}. Prognose über {glucose_alerts.HIGH_MGDL} um {_clock(warning['at'], zone)} "
                       f"(in etwa {minutes} min), höchster Wert in der nächsten Stunde etwa {warning['mgdl']:.0f}.")
        reminders.append({"key": f"{warning['kind']}:{now.astimezone(timezone.utc).isoformat()}",
                          "kind": warning["kind"], "title": LABELS[warning["kind"]],
                          "message": message + " Grobe Prognose aus Insulin, KH und Verlauf."})
    return reminders


def _today_inputs(now: datetime, settings: dict) -> tuple[list[dict], list[dict], list[dict]]:
    zone = ZoneInfo(settings["timezone"])
    today = now.astimezone(zone).date()
    # Today's day reaches back to yesterday afternoon, far enough for any open split plan.
    payload = day_view.day_payload(today.isoformat(), refresh=False)
    treatments = [{**row, "at": parse_instant(row["at"])} for row in payload["treatments"]]
    entries = [{"at": parse_instant(row["at"]), "sgv": row["sgv"]} for row in payload["entries"]]
    start = datetime.combine(today, time.min, tzinfo=zone)
    basal_today = basal_doses.basal_doses_between(start, start + timedelta(days=1))
    return treatments, basal_today, entries


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
    for reminder in (due_reminders(now, settings, treatments, basal_today, enabled)
                     + glucose_warnings(now, settings, treatments, entries, enabled)):
        if store.was_sent(reminder["key"]):
            continue
        last = store.last_sent(reminder["kind"]) if reminder["kind"] in WARNINGS else None
        if last and now - last < WARNING_REPEAT:
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
