"""Reminder to look at glucose and maybe correct after a meal.

Goal: keep an eye on glucose less, so a reminder that is about as good as one's own judgement is enough. Replayed on 128 main meals
(21.09.-07.11.) with the actual doses moved to the reminder: time in range 80.7 -> 81.4 %, > 180 16.4 -> 15.5 %,
< 70 2.8 -> 3.1 %, about 2 reminders a day. The app deciding the dose alone was worse (76.5 %), so the reminder
names the dose help's correction as orientation and the user decides.

From 90 min after a main meal until 7.5 h after it, with no open split plan (the split reminder covers that) and
no rapid insulin in the last 90 min: glucose at least 160 and the "Wirkt noch"
end value (glucose - insulin still acting + carbs and surcharge still to come) at least 160. Before 2 h only
from 180 and rising by 10 per 30 min, or from 200 whatever the trend. Earlier and lower is
mostly fast absorption, not more carbs. From 2 h the trend no longer counts (fewer conditions; replayed:
< 70 3.1 -> 3.5 %, < 54 0.6 -> 0.9 %, time in range 81.4 -> 81.1 %). At most once per 90 min.
"""
from __future__ import annotations

from app import core
from datetime import datetime, timedelta
from decimal import Decimal


FROM_MINUTES, TO_MINUTES = 90, 450
STRICT_UNTIL_MINUTES = 120
MIN_MGDL, END_MGDL = Decimal(160), Decimal(160)
EARLY_MGDL, EARLY_RISE_MGDL = Decimal(180), Decimal(10)
EARLY_HIGH_MGDL = Decimal(200)
RISE_MINUTES = 30
NO_INSULIN = timedelta(minutes=90)
REPEAT = timedelta(minutes=90)
FRESH_MINUTES = 10


def due(entries: list[dict], found: dict, treatments: list[dict], now: datetime) -> dict | None:
    """The reminder's numbers when it is due now, else None."""
    return core.call("correction_check.due", entries=entries, found=found, treatments=treatments, now=now)
