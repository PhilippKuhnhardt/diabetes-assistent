"""Small helpers shared by all layers: local days, instants, JSON values."""
from __future__ import annotations

import bisect
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from functools import lru_cache
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


class TravelZone(tzinfo):
    """The home zone with the switches Philipp made while travelling (user request 2026-11-07,
    4 weeks 6 h ahead from 2026-11-08): every instant reads the local clock of the zone in effect
    then, so meal kinds, the correction-factor schedule, warning hours and day boundaries follow
    the clock where he is. Behaves like a zone with large DST jumps: flying east skips local hours
    (the travel day is shorter), flying home repeats them (`fold` picks the later instant)."""

    def __init__(self, home: str, switches: tuple[tuple[datetime, str], ...]):
        self.key = home
        self._zones = [ZoneInfo(home)] + [ZoneInfo(name or home) for _, name in switches]
        self._starts = [at.astimezone(timezone.utc).replace(tzinfo=None) for at, _ in switches]

    def __repr__(self) -> str:
        return f"TravelZone({self.key!r}, {len(self._starts)} Wechsel)"

    def __str__(self) -> str:
        return self.key

    def _index(self, utc: datetime) -> int:
        return bisect.bisect_right(self._starts, utc)

    def zone_at(self, instant: datetime) -> ZoneInfo:
        return self._zones[self._index(instant.astimezone(timezone.utc).replace(tzinfo=None))]

    @lru_cache(maxsize=4096)
    def _instants(self, wall: datetime) -> tuple[datetime, ...]:
        """All UTC instants (naive) whose local clock reads `wall`, earliest first."""
        found = set()
        for index, zone in enumerate(self._zones):
            low = self._starts[index - 1] if index else None
            high = self._starts[index] if index < len(self._starts) else None
            for fold in (0, 1):
                utc = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc).replace(tzinfo=None)
                if (low is None or utc >= low) and (high is None or utc < high) \
                        and utc.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None) == wall:
                    found.add(utc)
        return tuple(sorted(found))

    def _zone_for_wall(self, dt: datetime) -> tuple[ZoneInfo, datetime]:
        wall = dt.replace(tzinfo=None, fold=0)
        instants = self._instants(wall)
        if instants:
            utc = instants[min(dt.fold, len(instants) - 1)]
            return self._zones[self._index(utc)], utc
        # A skipped local time (flying east): like PEP 495, fold 0 keeps the clock before the switch.
        for index, start in enumerate(self._starts):
            before = start.replace(tzinfo=timezone.utc).astimezone(self._zones[index]).replace(tzinfo=None)
            after = start.replace(tzinfo=timezone.utc).astimezone(self._zones[index + 1]).replace(tzinfo=None)
            if before <= wall < after:
                zone = self._zones[index + 1 if dt.fold else index]
                return zone, start
        return self._zones[0], wall

    def utcoffset(self, dt):
        if dt is None:
            return None
        zone, utc = self._zone_for_wall(dt)
        return zone.utcoffset(utc.replace(tzinfo=timezone.utc).astimezone(zone))

    def dst(self, dt):
        if dt is None:
            return None
        zone, utc = self._zone_for_wall(dt)
        return zone.dst(utc.replace(tzinfo=timezone.utc).astimezone(zone))

    def tzname(self, dt):
        if dt is None:
            return self.key
        zone, utc = self._zone_for_wall(dt)
        return zone.tzname(utc.replace(tzinfo=timezone.utc).astimezone(zone))

    def fromutc(self, dt):
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        utc = dt.replace(tzinfo=None)
        local = dt.replace(tzinfo=timezone.utc).astimezone(self._zones[self._index(utc)])
        wall = local.replace(tzinfo=None, fold=0)
        instants = self._instants(wall)
        fold = instants.index(utc) if utc in instants else local.fold
        return wall.replace(tzinfo=self, fold=min(fold, 1))


_zones: dict = {}


def local_zone(timezone_name: str) -> tzinfo:
    """The app's local zone: the home zone, or a TravelZone once Philipp switched zones."""
    from app.store import timezones  # the store imports this module

    switches = timezones.switches()
    if not switches:
        return ZoneInfo(timezone_name)
    key = (timezone_name, switches)
    if key not in _zones:
        _zones.clear()
        _zones[key] = TravelZone(timezone_name, switches)
    return _zones[key]


def zone_name_at(timezone_name: str, instant: datetime) -> str:
    """IANA name of the zone in effect at an instant (for the browser and the read API)."""
    local = local_zone(timezone_name)
    return local.zone_at(instant).key if isinstance(local, TravelZone) else timezone_name


def local_day_range(day: str, timezone_name: str) -> tuple[datetime, datetime]:
    parsed = date.fromisoformat(day)
    zone_ = local_zone(timezone_name)
    start = datetime.combine(parsed, time.min, tzinfo=zone_).astimezone(timezone.utc)
    # Include the following breakfast and a short CGM interpolation buffer.
    end = datetime.combine(parsed + timedelta(days=1), time(hour=13), tzinfo=zone_).astimezone(timezone.utc)
    return start - timedelta(hours=10), end


def parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Zeitpunkt benötigt eine Zeitzone")
    return parsed.astimezone(timezone.utc)


def unit_text(units: Decimal) -> str:
    """Insulin units without trailing zeros or exponent: 20, 17.5."""
    return format(units.normalize(), "f") if units else "0"
