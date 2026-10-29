"""Where a meal was eaten: matching a browser position to the spots Philipp defined for his places.

A place is a chip (Büro, Zuhause, Kantine); it has any number of spots, each a centre with a fixed
radius. One place can be the fallback (Auswärts): it is preselected when a precise position lies
in no spot. Positions only pick the chip and are never stored (user request 2026-10-29); a place
only labels entries, it changes no calculation until Philipp approves a use."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

EARTH_RADIUS_M = 6_371_000
# Every spot holds positions within this distance of its centre (a canteen, a restaurant).
SPOT_RADIUS_M = 150
# Coarser fixes (indoor Wi-Fi off, cell only) say too little about which canteen it was.
MAX_ACCURACY_M = 250
# A position belongs to an entry only when the entry is timed close to now and the fix was taken
# close to the entry time; a backdated entry was eaten somewhere else than where it is typed.
MAX_ENTRY_OFFSET = timedelta(minutes=30)
NAME_MAX = 40

STATUS_HINTS = {
    "matched": "per Standort erkannt",
    "away": "an keinem Standort",
    "unknown": "an keinem Standort, in den Einstellungen einen Ort als Standard für unterwegs festlegen",
    "inaccurate": "Standort zu ungenau",
    "offset": "Zeit weicht ab, Standort nicht verwendet",
    "none": "ohne Standort",
}


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def parse_position(raw) -> dict | None:
    """A browser position {lat, lon, accuracy_m[, captured_at]}; None when missing or invalid."""
    if not isinstance(raw, dict):
        return None
    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
        accuracy = float(raw.get("accuracy_m") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(accuracy)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 <= accuracy <= 100_000):
        return None
    captured = raw.get("captured_at")
    try:
        captured_at = datetime.fromisoformat(str(captured).replace("Z", "+00:00")) if captured else None
    except ValueError:
        captured_at = None
    return {"lat": round(lat, 6), "lon": round(lon, 6), "accuracy_m": round(accuracy), "captured_at": captured_at}


def usable(position: dict | None, at: datetime | None, now: datetime) -> str:
    """Whether a position may label an entry at `at`: 'ok' or the status saying why not."""
    if position is None or at is None:
        return "none"
    captured = position.get("captured_at")
    if abs(at - now) > MAX_ENTRY_OFFSET or (captured and abs(at - captured) > MAX_ENTRY_OFFSET):
        return "offset"
    if position["accuracy_m"] > MAX_ACCURACY_M:
        return "inaccurate"
    return "ok"


def match(position: dict, spots: list[dict]) -> dict | None:
    """The spot within SPOT_RADIUS_M of the position (the nearest when several do)."""
    inside = [(distance_m(position["lat"], position["lon"], spot["lat"], spot["lon"]), spot) for spot in spots]
    inside = [(distance, spot) for distance, spot in inside if distance <= SPOT_RADIUS_M]
    return min(inside, key=lambda item: item[0])[1] if inside else None


def suggestion(position: dict | None, at: datetime | None, now: datetime, spots: list[dict],
               fallback_id: int | None = None) -> dict:
    """What the entry form preselects: the place of the matching spot, the fallback place for a
    precise position in no spot, or none with the reason."""
    status = usable(position, at, now)
    if status != "ok":
        return {"status": status, "place_id": None, "hint": STATUS_HINTS[status]}
    spot = match(position, spots)
    if spot is None:
        status = "away" if fallback_id is not None else "unknown"
        return {"status": status, "place_id": fallback_id, "hint": STATUS_HINTS[status]}
    return {"status": "matched", "place_id": spot["place_id"], "hint": STATUS_HINTS["matched"]}


def validate_name(value) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    if not name:
        raise ValueError("Bitte einen Namen für den Ort eingeben")
    return name


def validate_spot(value: dict) -> dict:
    """Coordinates of one spot of a place: lat and lon, or typed text "49.0069, 8.4037" as a map
    app copies them."""
    lat, lon = value.get("lat"), value.get("lon")
    if value.get("coordinates") not in (None, ""):
        parts = str(value["coordinates"]).replace(";", ",").split(",")
        lat, lon = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (None, None)
    position = parse_position({"lat": lat, "lon": lon})
    if position is None:
        raise ValueError("Ungültige Koordinaten")
    return {"lat": position["lat"], "lon": position["lon"]}
