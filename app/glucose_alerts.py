"""Early warning of a low (below the low limit, default 70 mg/dL) or high (above the high limit, default
250 mg/dL) soon, daytime only.

Model: the "Wirkt noch"
model (insulin, carbs and surcharge still to act) plus the model's miss over the last 30 minutes, whose
rate fades out linearly over 60 minutes. Over 31 days it caught 12 of 12 daytime lows (median 17 min
ahead) and 11 of 12 highs (median 20 min ahead), about 4 warnings a day, about 1 of them needless.
Nights are left out: the sensor was unreliable there. No low warning within 30 min after a carb entry. A rough forecast, not dosing advice.

Review with 31 days: since the
CGM sends a reading every minute (12.10.) the miss is measured over 15 instead of 30 minutes and fades over
45 instead of 60, and the low pause follows only carb entries of at least 20 g, so a small carb correction
eaten because BG falls no longer mutes the warning. Daytime lows caught 16 -> 18 of 19 (median 20 -> 23 min
ahead), highs 10 -> 11 of 13, warnings 2.9 -> 3.0 a day.

Review: replayed every 2 min like the
background job, about 5.3 warnings a day came, about 4.3 of them without a low or high after. A low warning now needs
the crossing within 45 min, the miss fading over 25 min and BG at least 5 mg/dL lower than 10 min before; a high
warning needs the crossing within 30 min and BG at least 200. Daytime 21.09.-29.10.: lows caught 23 -> 22 of 24
(since 12.10. still 10 of 10, median 28 min ahead), highs 12 of 14 (median 18 -> 14 min ahead), warnings
5.3 -> 3.4 a day, those without an event 4.3 -> 2.5.

Pauses: no high warning for 30 min after rapid
insulin, since a dose just given barely acts yet and the forecast still extrapolates the rise (22.11. 21:00 local,
4 IE correction at BG 199, warned at 21:02 with a forecast of 256); no low warning for 15 min (before: 30) after any
carb entry.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.common import local_zone
from app import calculation, on_board

LOW_MGDL, HIGH_MGDL = Decimal(70), Decimal(250)
HORIZON_MINUTES = 60
STEP_MINUTES = 5
# How far back the model's miss is measured, and over how long its rate fades out (low warnings fade faster).
MISS_MINUTES, FADE_MINUTES, LOW_FADE_MINUTES = 15, Decimal(45), Decimal(25)
# A warning only when the forecast crosses this soon.
LOW_WITHIN_MINUTES, HIGH_WITHIN_MINUTES = 45, 30
# A low warning only while BG falls at least LOW_FALL_MGDL over the last LOW_FALL_MINUTES (a model-only fall that
# has not started yet was mostly a false alarm); a high warning only from HIGH_FROM_BELOW below the high limit
# (200 with the default 250).
LOW_FALL_MINUTES, LOW_FALL_MGDL = 10, Decimal(5)
HIGH_FROM_BELOW = Decimal(50)
# Warnings only from DAY_FROM to DAY_TO local time (hours).
DAY_FROM, DAY_TO = 6, 23
# The latest CGM reading must be at most this old.
FRESH_MINUTES = 10
# No low warning this soon after a carb entry: the model lets carbs act at once, real absorption starts
# later, so right after eating the "faster than expected" fall is mostly that lag (24 of ~100 low
# warnings came within 45 min after carbs, 2 before a real low; a 30-min pause kept 12 of 12 lows).
# From 2026-10-22 to 2026-11-22 only carb entries
# of at least 20 g paused it (a 10 g correction on 18.10. 12:17 had muted a warning before a low at 12:31); since 2026-11-22 every carb entry pauses it again, 15 min by default,
# set in the notification settings.
LOW_PAUSE_MINUTES = 15
# No high warning this soon after rapid insulin: the dose just given is meant to bring BG
# down, but barely acts within the warning's 30 min, so the warning would only repeat what the user just acted on.
HIGH_PAUSE_MINUTES = 30


def _faded(minutes: Decimal, fade: Decimal = FADE_MINUTES) -> Decimal:
    """Minutes' worth of the miss rate still added after `minutes`, with the rate fading linearly to 0."""
    k = min(minutes, fade)
    return k - k * k / (2 * fade)


def _earlier(entries: list[dict], at: datetime) -> Decimal | None:
    try:
        return calculation.interpolate_glucose(entries, at)["value"]
    except ValueError:
        return None


def forecast(entries: list[dict], found: dict, now: datetime, fade: Decimal = FADE_MINUTES) -> dict | None:
    """Forecast every 5 minutes for the next hour from the latest fresh CGM reading, or None."""
    readings = [entry for entry in entries if entry.get("sgv") is not None and entry["at"] <= now]
    if not readings:
        return None
    latest = max(readings, key=lambda entry: entry["at"])
    start, glucose = latest["at"], calculation.dec(latest["sgv"])
    if now - start > timedelta(minutes=FRESH_MINUTES):
        return None
    earlier = _earlier(entries, start - timedelta(minutes=MISS_MINUTES))
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
                 + (ahead["raising_mgdl"] - later["raising_mgdl"]) + rate * _faded(Decimal(minutes), fade))
        points.append({"at": start + timedelta(minutes=minutes), "mgdl": value})
    return {"at": start, "glucose_mgdl": glucose, "miss_per_hour": rate * 60, "points": points}


def _since(rows: list[dict], now: datetime, pause_minutes: int) -> bool:
    """Whether one of the rows (with "at") lies within the last pause_minutes before now."""
    return any(now - timedelta(minutes=pause_minutes) < row["at"] <= now for row in rows)


def warnings(entries: list[dict], found: dict, now: datetime, timezone_name: str,
             low_pause_minutes: int = LOW_PAUSE_MINUTES, high_pause_minutes: int = HIGH_PAUSE_MINUTES,
             low_mgdl: Decimal = LOW_MGDL, high_mgdl: Decimal = HIGH_MGDL) -> list[dict]:
    """The low and high warnings due now: kind, the glucose now, when and how far the forecast crosses.
    No low warning within low_pause_minutes after a carb entry, no high warning within
    high_pause_minutes after rapid insulin (0 = no pause)."""
    local = now.astimezone(local_zone(timezone_name))
    if not DAY_FROM <= local.hour < DAY_TO:
        return []
    result, low_result = forecast(entries, found, now), forecast(entries, found, now, LOW_FADE_MINUTES)
    if result is None:
        return []
    due, glucose = [], result["glucose_mgdl"]
    before = _earlier(entries, result["at"] - timedelta(minutes=LOW_FALL_MINUTES))
    falling = before is None or glucose - before <= -LOW_FALL_MGDL
    paused = _since(found["carbs"], now, low_pause_minutes)
    low_points = [point for point in low_result["points"] if point["at"] <= result["at"] + timedelta(minutes=LOW_WITHIN_MINUTES)]
    low = next((point for point in low_points if point["mgdl"] < low_mgdl), None)
    if low and glucose >= low_mgdl and falling and not paused:
        due.append({"kind": "low", "glucose_mgdl": glucose, "at": low["at"],
                    "mgdl": min(point["mgdl"] for point in low_points)})
    high_points = [point for point in result["points"] if point["at"] <= result["at"] + timedelta(minutes=HIGH_WITHIN_MINUTES)]
    high = next((point for point in high_points if point["mgdl"] > high_mgdl), None)
    if high and high_mgdl - HIGH_FROM_BELOW <= glucose <= high_mgdl and not _since(found["doses"], now, high_pause_minutes):
        due.append({"kind": "high", "glucose_mgdl": glucose, "at": high["at"],
                    "mgdl": max(point["mgdl"] for point in high_points)})
    return due
