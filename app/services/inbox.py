"""Hints on the diary's start page: things that need an action from Philipp (user request 2026-10-21).

A general list so later kinds (say, a better factor found) can join. The first kind is a finished
meal that is still open in "Tag durchgehen": an undecided sport suggestion or a factor that cannot be
calculated. A hint disappears once its action is done elsewhere (it is simply no longer open) and
loses its highlight once opened; a meal that becomes open for a new reason is highlighted again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.common import parse_instant
from app.episodes import SPORT_LABELS
from app.services import day_view
from app.store import config
from app.store import inbox as store

LOOKBACK_DAYS = 7


def meal_items(day: dict, now: datetime, zone: ZoneInfo) -> list[dict]:
    """Finished meals of one calculated day that still need a look."""
    items = []
    for meal in day.get("meals", []):
        open_items = (meal.get("review") or {}).get("open") or []
        # Excluded meals no longer count, and a meal still running is not done yet.
        if not open_items or meal.get("excluded") or parse_instant(meal["window"]["end"]) > now:
            continue
        reasons = []
        if "sport" in open_items:
            events = ", ".join(SPORT_LABELS.get(item.get("kind"), "Sport") + " "
                               + parse_instant(item["at"]).astimezone(zone).strftime("%H:%M")
                               for item in meal["sport"].get("events", []))
            reasons.append({"kind": "sport", "text": "Sport berücksichtigen" + (f" ({events})" if events else "")})
        if "blocked" in open_items:
            reasons.append({"kind": "blocked", "text": "Faktor nicht berechenbar: "
                            + (meal["calculation"].get("reason") or "Grund unbekannt")})
        items.append({
            "key": f"meal:{meal['id']}:{','.join(sorted(open_items))}", "kind": "meal",
            "day": day["day"], "meal_id": meal["id"], "meal": meal["meal"], "at": meal["at"],
            "reasons": reasons,
        })
    return items


def view(params: dict | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    _, settings = config.get_settings()
    zone = ZoneInfo(settings["timezone"])
    today = now.astimezone(zone).date()
    items = []
    for offset in range(LOOKBACK_DAYS):
        items += meal_items(day_view.day_payload((today - timedelta(days=offset)).isoformat(), refresh=False), now, zone)
    items.sort(key=lambda item: parse_instant(item["at"]), reverse=True)
    seen = store.seen_keys([item["key"] for item in items])
    for item in items:
        item["seen"] = item["key"] in seen
    return {"items": items, "new": sum(not item["seen"] for item in items)}


def mark_seen(payload: dict) -> dict:
    key = payload.get("key")
    if not isinstance(key, str) or not key or len(key) > 300:
        raise ValueError("Hinweis fehlt")
    store.mark_seen(key)
    return view()
