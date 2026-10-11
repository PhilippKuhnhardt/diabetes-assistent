"""Early warning of a low (< 70 mg/dL) or high (> 250 mg/dL) within the next hour, daytime only.

Analysis and approval 2026-10-11 (project files, analysen/fruehprognose.md, section 5): the "Wirkt noch"
model (insulin, carbs and surcharge still to act) plus the model's miss over the last 30 minutes, whose
rate fades out linearly over 60 minutes. Over 31 days it caught 12 of 12 daytime lows (median 17 min
ahead) and 11 of 12 highs (median 20 min ahead), about 4 warnings a day, about 1 of them needless.
Nights are left out: the sensor was unreliable there. No low warning within 30 min after a carb entry. A rough forecast, not dosing advice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation, on_board

LOW_MGDL, HIGH_MGDL = Decimal(70), Decimal(250)
HORIZON_MINUTES = 60
STEP_MINUTES = 5
# How far back the model's miss is measured, and over how long its rate fades out.
MISS_MINUTES, FADE_MINUTES = 30, Decimal(60)
# Warnings only from DAY_FROM to DAY_TO local time (hours).
DAY_FROM, DAY_TO = 6, 23
# The latest CGM reading must be at most this old.
FRESH_MINUTES = 10
# No low warning this soon after a carb entry: the model lets carbs act at once, real absorption starts
# later, so right after eating the "faster than expected" fall is mostly that lag (24 of ~100 low
# warnings came within 45 min after carbs, 2 before a real low; a 30-min pause kept 12 of 12 lows,
# user report 2026-10-11, analysen/fruehprognose.md section 7).
LOW_PAUSE_AFTER_CARBS = timedelta(minutes=30)


def _faded(minutes: Decimal) -> Decimal:
    """Minutes' worth of the miss rate still added after `minutes`, with the rate fading linearly to 0."""
    k = min(minutes, FADE_MINUTES)
    return k - k * k / (2 * FADE_MINUTES)


def forecast(entries: list[dict], found: dict, now: datetime) -> dict | None:
    """Forecast every 5 minutes for the next hour from the latest fresh CGM reading, or None."""
    readings = [entry for entry in entries if entry.get("sgv") is not None and entry["at"] <= now]
    if not readings:
        return None
    latest = max(readings, key=lambda entry: entry["at"])
    start, glucose = latest["at"], calculation.dec(latest["sgv"])
    if now - start > timedelta(minutes=FRESH_MINUTES):
        return None
    try:
        earlier = calculation.interpolate_glucose(entries, start - timedelta(minutes=MISS_MINUTES))["value"]
    except ValueError:
        earlier = None
    at_start = on_board.effects_at(found, start)
    rate = Decimal(0)
    if earlier is not None:
        before = on_board.effects_at(found, start - timedelta(minutes=MISS_MINUTES), start)
        modelled = -(before["lowering_mgdl"] - at_start["lowering_mgdl"]) + (before["raising_mgdl"] - at_start["raising_mgdl"])
        rate = ((glucose - earlier) - modelled) / MISS_MINUTES
    # Ahead, entries made after the reading (up to now) count too.
    ahead, points = on_board.effects_at(found, start, now), []
    for minutes in range(STEP_MINUTES, HORIZON_MINUTES + 1, STEP_MINUTES):
        later = on_board.effects_at(found, start + timedelta(minutes=minutes), now)
        value = (glucose - (ahead["lowering_mgdl"] - later["lowering_mgdl"])
                 + (ahead["raising_mgdl"] - later["raising_mgdl"]) + rate * _faded(Decimal(minutes)))
        points.append({"at": start + timedelta(minutes=minutes), "mgdl": value})
    return {"at": start, "glucose_mgdl": glucose, "miss_per_hour": rate * 60, "points": points}


def warnings(entries: list[dict], found: dict, now: datetime, timezone_name: str) -> list[dict]:
    """The low and high warnings due now: kind, the glucose now, when and how far the forecast crosses."""
    local = now.astimezone(ZoneInfo(timezone_name))
    if not DAY_FROM <= local.hour < DAY_TO:
        return []
    result = forecast(entries, found, now)
    if result is None:
        return []
    due, glucose = [], result["glucose_mgdl"]
    last_carbs = max((row["at"] for row in found["carbs"] if row["at"] <= now), default=None)
    paused = last_carbs is not None and now - last_carbs < LOW_PAUSE_AFTER_CARBS
    low = next((point for point in result["points"] if point["mgdl"] < LOW_MGDL), None)
    if low and glucose >= LOW_MGDL and not paused:
        due.append({"kind": "low", "glucose_mgdl": glucose, "at": low["at"],
                    "mgdl": min(point["mgdl"] for point in result["points"])})
    high = next((point for point in result["points"] if point["mgdl"] > HIGH_MGDL), None)
    if high and glucose <= HIGH_MGDL:
        due.append({"kind": "high", "glucose_mgdl": glucose, "at": high["at"],
                    "mgdl": max(point["mgdl"] for point in result["points"])})
    return due
