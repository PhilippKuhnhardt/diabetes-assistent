"""Current Nightscout CGM status for the persistent header."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation


def current_status(entries: list[dict], settings: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    points = sorted(
        (item["at"], calculation.dec(item["sgv"]))
        for item in entries
        if item.get("sgv") is not None and now - timedelta(hours=1) <= item["at"] <= now
    )
    if not points:
        return {"glucose_mgdl": None, "glucose_at": None, "fresh": False,
                "status": "unavailable", "trend_delta_mgdl": None, "trend_minutes": None,
                "target_low_mgdl": None, "target_high_mgdl": None}

    latest_at, value = points[-1]
    fresh = now - latest_at <= timedelta(minutes=15)
    prior = next(((at, glucose) for at, glucose in reversed(points[:-1])
                  if timedelta(minutes=5) <= latest_at - at <= timedelta(minutes=20)), None)
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
        "trend_delta_mgdl": str(value - prior[1]) if fresh and prior else None,
        "trend_minutes": str(calculation.minutes_between(latest_at, prior[0])) if fresh and prior else None,
        "target_low_mgdl": str(low) if low is not None else None,
        "target_high_mgdl": str(high) if high is not None else None,
    }
