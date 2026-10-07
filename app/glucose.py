"""Current Nightscout CGM status for the persistent header."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation

TREND_WINDOW_MINUTES = Decimal(15)
TREND_FLAT_MGDL = Decimal(10)
TREND_FAST_MGDL = Decimal(30)
LOW_MGDL = Decimal(70)
HIGH_MGDL = Decimal(250)


def target_at(at: datetime, settings: dict) -> dict | None:
    """The Nightscout target range row in force at the local clock time of `at`."""
    local = at.astimezone(ZoneInfo(settings["timezone"]))
    minute = local.hour * 60 + local.minute
    return max((row for row in settings.get("target_schedule") or [] if int(row["start_minute"]) <= minute),
               key=lambda row: int(row["start_minute"]), default=None)


def tone(value, at: datetime, settings: dict) -> str:
    """Colour class of a CGM value: low < 70, high > 250, else inside or outside the target range."""
    if value is None:
        return "none"
    value = calculation.dec(value)
    if value < LOW_MGDL:
        return "low"
    if value > HIGH_MGDL:
        return "high"
    target = target_at(at, settings)
    if target is None:
        return "neutral"
    inside = calculation.dec(target["low_mgdl"]) <= value <= calculation.dec(target["high_mgdl"])
    return "in-range" if inside else "outside"


def target_bands(start: datetime, end: datetime, settings: dict) -> list[dict]:
    """The target range as time segments between two instants, for drawing behind a curve."""
    zone = ZoneInfo(settings["timezone"])
    rows = sorted(settings.get("target_schedule") or [], key=lambda row: int(row["start_minute"]))
    bands = []
    day = start.astimezone(zone).date() - timedelta(days=1)
    while day <= end.astimezone(zone).date():
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=zone)
        for index, row in enumerate(rows):
            # Local clock times; the next row (or the next midnight) ends the segment.
            begin = (midnight + timedelta(minutes=int(row["start_minute"]))).astimezone(timezone.utc)
            stop_minute = int(rows[index + 1]["start_minute"]) if index + 1 < len(rows) else 1440
            stop = (midnight + timedelta(minutes=stop_minute)).astimezone(timezone.utc)
            if stop <= start or begin >= end:
                continue
            band = {"from": max(begin, start), "to": min(stop, end),
                    "low_mgdl": str(row["low_mgdl"]), "high_mgdl": str(row["high_mgdl"])}
            if bands and bands[-1]["to"] == band["from"] and (bands[-1]["low_mgdl"], bands[-1]["high_mgdl"]) == (
                    band["low_mgdl"], band["high_mgdl"]):
                bands[-1]["to"] = band["to"]
            else:
                bands.append(band)
        day += timedelta(days=1)
    return bands


def trend(points: list[tuple[datetime, Decimal]]) -> dict | None:
    """Least-squares slope over the CGM values of the last 15 minutes up to the latest value,
    expressed as the change per 15 minutes. Needs at least 5 minutes of course; a change below
    5 mg/dL per 15 minutes counts as flat, from 30 mg/dL (2 mg/dL/min) as fast."""
    if not points:
        return None
    latest_at = points[-1][0]
    window = [(-calculation.minutes_between(latest_at, at), value) for at, value in points
              if latest_at - at <= timedelta(minutes=16)]
    span = -window[0][0] if window else Decimal(0)
    if len(window) < 2 or span < 5:
        return None
    mean_x = sum(x for x, _ in window) / len(window)
    mean_y = sum(y for _, y in window) / len(window)
    slope = (sum((x - mean_x) * (y - mean_y) for x, y in window)
             / sum((x - mean_x) ** 2 for x, _ in window))
    delta = (slope * TREND_WINDOW_MINUTES).quantize(Decimal("0.1"))
    if abs(delta) < TREND_FLAT_MGDL:
        direction = "flat"
    elif delta > 0:
        direction = "up_fast" if delta >= TREND_FAST_MGDL else "up"
    else:
        direction = "down_fast" if delta <= -TREND_FAST_MGDL else "down"
    return {"trend_delta_mgdl": str(delta), "trend_minutes": str(TREND_WINDOW_MINUTES),
            "trend_direction": direction}


NO_TREND = {"trend_delta_mgdl": None, "trend_minutes": None, "trend_direction": None}


def current_status(entries: list[dict], settings: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    points = sorted(
        (item["at"], calculation.dec(item["sgv"]))
        for item in entries
        if item.get("sgv") is not None and now - timedelta(hours=1) <= item["at"] <= now
    )
    if not points:
        return {"glucose_mgdl": None, "glucose_at": None, "fresh": False,
                "status": "unavailable", **NO_TREND,
                "target_low_mgdl": None, "target_high_mgdl": None}

    latest_at, value = points[-1]
    fresh = now - latest_at <= timedelta(minutes=15)
    course = (trend(points) if fresh else None) or NO_TREND
    schedule = settings.get("target_schedule") or []
    local = now.astimezone(ZoneInfo(settings["timezone"]))
    minute = local.hour * 60 + local.minute
    target = max((row for row in schedule if int(row["start_minute"]) <= minute),
                 key=lambda row: int(row["start_minute"]), default=None)
    low = calculation.dec(target["low_mgdl"]) if target else None
    high = calculation.dec(target["high_mgdl"]) if target else None
    if not fresh:
        status = "stale"
    elif value < Decimal(70):
        status = "low"
    elif value > Decimal(250):
        status = "high"
    elif low is None or high is None:
        status = "no_target"
    elif low <= value <= high:
        status = "in_range"
    else:
        status = "outside"
    return {
        "glucose_mgdl": str(value), "glucose_at": latest_at.isoformat(),
        "fresh": fresh, "status": status,
        **course,
        "target_low_mgdl": str(low) if low is not None else None,
        "target_high_mgdl": str(high) if high is not None else None,
    }
