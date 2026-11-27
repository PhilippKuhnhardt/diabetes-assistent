"""Current Nightscout CGM status for the persistent header."""
from __future__ import annotations

from app import core
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from app.common import local_zone
from app import calculation
from app.i18n import english


TREND_WINDOW_MINUTES = Decimal(15)
TREND_FLAT_MGDL = Decimal(10)
TREND_FAST_MGDL = Decimal(30)
LOW_MGDL = Decimal(70)
HIGH_MGDL = Decimal(250)


def limits(settings: dict | None) -> tuple[Decimal, Decimal]:
    """The low and high limit in mg/dL (settings low_limit_mgdl / high_limit_mgdl, default 70 / 250)."""
    return core.call("glucose.limits", settings=settings)


MGDL_PER_MMOL = Decimal("18.0182")


def shown(mgdl, settings: dict | None, signed: bool = False) -> str:
    """A glucose value or change (mg/dL) as push messages show it: whole mg/dL, or mmol/L with one decimal
    (settings glucose_unit), decimal comma in German."""
    return core.call("glucose.shown", mgdl=mgdl, settings=settings, signed=signed)


def target_at(at: datetime, settings: dict) -> dict | None:
    """The Nightscout target range row in force at the local clock time of `at`."""
    return core.call("glucose.target_at", at=at, settings=settings)


def tone(value, at: datetime, settings: dict) -> str:
    """Colour class of a CGM value: below the low limit, above the high limit, else inside or outside the
    target range."""
    return core.call("glucose.tone", value=value, at=at, settings=settings)


def target_bands(start: datetime, end: datetime, settings: dict) -> list[dict]:
    """The target range as time segments between two instants, for drawing behind a curve."""
    return core.call("glucose.target_bands", start=start, end=end, settings=settings)


def trend(points: list[tuple[datetime, Decimal]]) -> dict | None:
    """Least-squares slope over the CGM values of the last 15 minutes up to the latest value,
    expressed as the change per 15 minutes. Needs at least 5 minutes of course; a change below
    5 mg/dL per 15 minutes counts as flat, from 30 mg/dL (2 mg/dL/min) as fast."""
    return core.call("glucose.trend", points=points)


NO_TREND = {"trend_delta_mgdl": None, "trend_minutes": None, "trend_direction": None}


def current_status(entries: list[dict], settings: dict, now: datetime | None = None) -> dict:
    now = core.now_or(now)
    return core.call("glucose.current_status", entries=entries, settings=settings, now=now)
