"""Push reminders over Pushover: a forgotten basal and a due second split part;
since 2026-10-11 also early warnings of a low or high within the next hour (app/glucose_alerts.py), and since
2026-11-14 a reminder to look at glucose and maybe correct after a meal (app/correction_check.py).

The background job checks them after each sync round; each reminder is sent once (its key is stored)
and only after a successful Nightscout sync, so a dose already given but not yet mirrored cannot
trigger a false reminder. A low or high warning repeats at most once an hour, "BZ prüfen" every 90 min.
Each kind has its own Pushover priority.

Which reminders are due and what they say is decided in the Rust core (notifications.due,
core/diabetes-core/src/notifications.rs); this module is the adapter around it: it gathers the dose help's
snapshot, today's basal and the stored switches, pauses and send times, and sends over Pushover.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from app import core, glucose_alerts
from app.common import local_zone
from app.ingestion import pushover
from app.services import dose_help
from app.store import basal_doses, config
from app.store import notifications as store
from app.i18n import tr

KINDS = {"basal": True, "split": True, "low": True, "high": True, "check": True}
LABELS = {"basal": tr("Basal vergessen"), "split": tr("2. Split-Teil fällig"), "low": tr("Tief droht"), "high": tr("Hoch droht"),
          "check": tr("BZ prüfen")}
# Kinds that may repeat (each after its own pause, notifications.rs repeat_minutes) rather than once per key.
REPEATING = ("low", "high", "check")
# Pushover priorities (https://pushover.net/api#priority); default 1 skips the quiet hours as before.
PRIORITY_LABELS = {-2: tr("Ganz leise (nur in der Pushover-App)"), -1: tr("Leise (ohne Ton)"), 0: tr("Normal"),
                   1: tr("Hoch (auch in Ruhezeiten)"), 2: tr("Notfall (wiederholt bis bestätigt)")}
PRIORITIES = {kind: pushover.PRIORITY for kind in KINDS}
# Minutes without a warning: low after a carb entry, high after rapid insulin.
PAUSES = {"low": glucose_alerts.LOW_PAUSE_MINUTES, "high": glucose_alerts.HIGH_PAUSE_MINUTES}
MAX_PAUSE_MINUTES = 180


def _reminders(rows: list[dict]) -> list[dict]:
    return [{"key": row["key"], "kind": row["kind"], "title": row["title"], "message": row["message"]} for row in rows]


def due_reminders(now: datetime, settings: dict, treatments: list[dict], basal_today: list[dict],
                  enabled: dict[str, bool], entries: list[dict] | None = None,
                  second_part: Decimal | None = None) -> list[dict]:
    """Basal and split reminders due now, before checking which were already sent. second_part is the
    second split part's IE from the current forecast (None without a fresh glucose value)."""
    return _reminders(core.call("notifications.due_reminders", now=now, settings=settings, treatments=treatments,
                                basal_today=bool(basal_today), enabled=enabled, entries=entries or [],
                                second_part=second_part))


def warning_reminders(now: datetime, settings: dict, warnings: list[dict], enabled: dict[str, bool]) -> list[dict]:
    """The push texts of low and high warnings (glucose_alerts.warnings), switched-on kinds only."""
    return _reminders(core.call("notifications.warning_reminders", now=now, settings=settings, warnings=warnings,
                                enabled=enabled))


def correction_reminder(now: datetime, settings: dict, hit: dict, correction: dict | None) -> dict:
    """The "BZ prüfen" text for a due check (correction_check.due) with the dose help's correction:
    normal and cautious IE, and after a recent meal the ends fast/slow with the tempo the rise leans to."""
    return _reminders([core.call("notifications.correction_reminder", now=now, settings=settings, hit=hit,
                                 correction=correction)])[0]


def _inputs(now: datetime, enabled: dict[str, bool]) -> dict:
    """What notifications.due reads: the dose help's snapshot of now plus today's basal, pauses and repeats."""
    _, settings = config.get_settings()
    zone = local_zone(settings["timezone"])
    start = datetime.combine(now.astimezone(zone).date(), time.min, tzinfo=zone)
    return {
        "snapshot": dose_help.snapshot({"carbs": "0"}, now),
        "enabled": enabled,
        "basal_today": bool(basal_doses.basal_doses_between(start, start + timedelta(days=1))),
        "pauses": store.pauses(PAUSES),
        "last_sent": {kind: store.last_sent(kind) for kind in REPEATING},
    }


def check(now: datetime | None = None) -> list[dict]:
    """Send every due reminder once. Returns what was sent; a failed send is retried next round."""
    if not pushover.is_configured():
        return []
    now = now or datetime.now(timezone.utc)
    enabled = store.enabled_kinds(KINDS)
    if not any(enabled.values()):
        return []
    priorities = store.priorities(PRIORITIES)
    sent = []
    for reminder in _reminders(core.call("notifications.due", **_inputs(now, enabled))):
        if store.was_sent(reminder["key"]):
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
