"""Push reminders over Pushover (user decision 2026-10-09): a forgotten basal and a due second split part.

The background job checks them after each sync round; each reminder is sent once (its key is stored)
and only after a successful Nightscout sync, so a dose already given but not yet mirrored cannot
trigger a false reminder.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import calculation, split
from app.common import parse_instant, unit_text
from app.ingestion import pushover
from app.services import day_view
from app.store import basal_doses, config
from app.store import notifications as store

KINDS = {"basal": True, "split": True}
LABELS = {"basal": "Basal vergessen", "split": "2. Split-Teil fällig"}


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


def _today_inputs(now: datetime, settings: dict) -> tuple[list[dict], list[dict]]:
    zone = ZoneInfo(settings["timezone"])
    today = now.astimezone(zone).date()
    # Today's day reaches back to yesterday afternoon, far enough for any open split plan.
    payload = day_view.day_payload(today.isoformat(), refresh=False)
    treatments = [{**row, "at": parse_instant(row["at"])} for row in payload["treatments"]]
    start = datetime.combine(today, time.min, tzinfo=zone)
    basal_today = basal_doses.basal_doses_between(start, start + timedelta(days=1))
    return treatments, basal_today


def check(now: datetime | None = None) -> list[dict]:
    """Send every due reminder once. Returns what was sent; a failed send is retried next round."""
    if not pushover.is_configured():
        return []
    now = now or datetime.now(timezone.utc)
    enabled = store.enabled_kinds(KINDS)
    if not any(enabled.values()):
        return []
    _, settings = config.get_settings()
    treatments, basal_today = _today_inputs(now, settings)
    sent = []
    for reminder in due_reminders(now, settings, treatments, basal_today, enabled):
        if store.was_sent(reminder["key"]):
            continue
        pushover.send(reminder["title"], reminder["message"])
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
        "basal_after": f"{minute // 60:02d}:{minute % 60:02d}",
        "recent": [{**row, "label": LABELS.get(row["kind"], row["title"])} for row in store.recent()],
    }


def set_enabled(payload: dict) -> dict:
    kind, enabled = payload.get("kind"), payload.get("enabled")
    if kind not in KINDS or not isinstance(enabled, bool):
        raise ValueError("Ungültige Benachrichtigung")
    store.set_enabled(kind, enabled)
    return view()


def send_test(payload: dict) -> dict:
    try:
        pushover.send("Testmeldung", "Benachrichtigungen vom Diabetes-Assistenten kommen an.")
    except pushover.PushoverError as error:
        raise ValueError(str(error)) from None
    return {"message": "Testmeldung gesendet"}
