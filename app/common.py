"""Small helpers shared by all layers: local days, instants, JSON values."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo



MEALS = {"breakfast", "lunch", "dinner"}


def json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    return value


def local_day_range(day: str, timezone_name: str) -> tuple[datetime, datetime]:
    parsed = date.fromisoformat(day)
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(parsed, time.min, tzinfo=zone).astimezone(timezone.utc)
    # Include the following breakfast and a short CGM interpolation buffer.
    end = datetime.combine(parsed + timedelta(days=1), time(hour=13), tzinfo=zone).astimezone(timezone.utc)
    return start - timedelta(hours=10), end


def parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Zeitpunkt benötigt eine Zeitzone")
    return parsed.astimezone(timezone.utc)


def unit_text(units: Decimal) -> str:
    """Insulin units without trailing zeros or exponent: 20, 17.5."""
    return format(units.normalize(), "f") if units else "0"
