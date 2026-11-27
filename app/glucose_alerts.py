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

from app import core
from datetime import datetime
from decimal import Decimal


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


def forecast(entries: list[dict], found: dict, now: datetime, fade: Decimal = FADE_MINUTES) -> dict | None:
    """Forecast every 5 minutes for the next hour from the latest fresh CGM reading, or None."""
    return core.call("glucose_alerts.forecast", entries=entries, found=found, now=now, fade=fade)


def warnings(entries: list[dict], found: dict, now: datetime, timezone_name: str,
             low_pause_minutes: int = LOW_PAUSE_MINUTES, high_pause_minutes: int = HIGH_PAUSE_MINUTES,
             low_mgdl: Decimal = LOW_MGDL, high_mgdl: Decimal = HIGH_MGDL) -> list[dict]:
    """The low and high warnings due now: kind, the glucose now, when and how far the forecast crosses.
    No low warning within low_pause_minutes after a carb entry, no high warning within
    high_pause_minutes after rapid insulin (0 = no pause)."""
    return core.call(
        "glucose_alerts.warnings",
        entries=entries, found=found, now=now, timezone_name=timezone_name, low_pause_minutes=low_pause_minutes, high_pause_minutes=high_pause_minutes, low_mgdl=low_mgdl, high_mgdl=high_mgdl)
