"""Reminder to look at glucose and maybe correct after a meal (user request and approval 2026-11-14).

Analysis in the project files, analysen/nachspritzen-2026-11-14.md (Nachtrag): Philipp wants to keep an eye on
glucose less, so a reminder that is about as good as his own judgement is enough. Replayed on 128 main meals
(21.09.-07.11.) with his own doses moved to the reminder: time in range 80.7 -> 81.4 %, > 180 16.4 -> 15.5 %,
< 70 2.8 -> 3.1 %, about 2 reminders a day. The app deciding the dose alone was worse (76.5 %), so the reminder
names the dose help's correction as orientation and Philipp decides.

From 90 min after a main meal until 7.5 h after it, with no open split plan (the split reminder covers that) and
no rapid insulin in the last 90 min: glucose at least 160 and not lower than 30 min before, and the "Wirkt noch"
end value (glucose - insulin still acting + carbs and surcharge still to come) at least 160. Before 2 h only
from 180 and rising by 10 per 30 min, or from 200 whatever the trend (user report 2026-11-15: 234 falling from 266
at 97 min stayed silent; replayed it changes nothing, analysen/nachspritzen-2026-11-14/a9.py). Earlier and lower is
mostly fast absorption, not more carbs. At most once per 90 min.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app import calculation, on_board, split

FROM_MINUTES, TO_MINUTES = 90, 450
STRICT_UNTIL_MINUTES = 120
MIN_MGDL, END_MGDL = Decimal(160), Decimal(160)
EARLY_MGDL, EARLY_RISE_MGDL = Decimal(180), Decimal(10)
EARLY_HIGH_MGDL = Decimal(200)
RISE_MINUTES = 30
NO_INSULIN = timedelta(minutes=90)
REPEAT = timedelta(minutes=90)
FRESH_MINUTES = 10


def _glucose_at(entries: list[dict], at: datetime) -> Decimal | None:
    try:
        return calculation.interpolate_glucose(entries, at)["value"]
    except ValueError:
        return None


def due(entries: list[dict], found: dict, treatments: list[dict], now: datetime) -> dict | None:
    """The reminder's numbers when it is due now, else None."""
    meals = [item["at"] for item in treatments if on_board.is_main_meal(item) and item["at"] <= now]
    if not meals:
        return None
    meal_at = max(meals)
    minutes = (now - meal_at).total_seconds() / 60
    if not FROM_MINUTES <= minutes <= TO_MINUTES or split.open_plan(treatments, now):
        return None
    if any(now - NO_INSULIN < item["at"] <= now and calculation.is_rapid_insulin(item)
           and calculation.dec(item.get("insulin") or 0) > 0 for item in treatments):
        return None
    readings = [entry for entry in entries if entry.get("sgv") is not None and entry["at"] <= now]
    if not readings:
        return None
    latest = max(readings, key=lambda entry: entry["at"])
    if now - latest["at"] > timedelta(minutes=FRESH_MINUTES):
        return None
    glucose = calculation.dec(latest["sgv"])
    before = _glucose_at(entries, latest["at"] - timedelta(minutes=RISE_MINUTES))
    if before is None:
        return None
    rise = glucose - before
    if minutes < STRICT_UNTIL_MINUTES:
        if glucose < EARLY_HIGH_MGDL and (glucose < EARLY_MGDL or rise < EARLY_RISE_MGDL):
            return None
    elif glucose < MIN_MGDL or rise < 0:
        return None
    acting = on_board.effects_at(found, latest["at"], now)
    end = glucose - acting["lowering_mgdl"] + acting["raising_mgdl"]
    if end < END_MGDL:
        return None
    return {"meal_at": meal_at, "glucose_mgdl": glucose, "rise_mgdl": rise, "end_mgdl": end}
