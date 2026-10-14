"""Where a meal was eaten: matching a browser position to the user's places, and grouping
positions that match no place yet so they can become one.

A place is a chip (Kantine, Restaurant, Zuhause); it has any number of spots, each a centre with
a radius, so one chip can gather many restaurants while a regular one keeps its own chip.

Positions stay in SQLite; Nightscout never sees them. A place only labels entries, it changes
no calculation until Philipp approves a use."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

EARTH_RADIUS_M = 6_371_000
DEFAULT_RADIUS_M = 150
RADIUS_LIMITS = (30, 1000)
# Coarser fixes (indoor Wi-Fi off, cell only) say too little about which canteen it was.
MAX_ACCURACY_M = 250
# A position belongs to an entry only when the entry is timed close to now; a backdated entry
# was eaten somewhere else than where it is typed.
MAX_ENTRY_OFFSET = timedelta(minutes=30)
MAX_POSITION_AGE = timedelta(minutes=5)
# Unknown positions closer than this to a group's centre join that group.
CLUSTER_RADIUS_M = 150
NAME_MAX = 40

STATUS_HINTS = {
    "matched": "per Standort erkannt",
    "unknown": "Standort ist noch kein Ort, später in den Einstellungen benennbar",
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
    if abs(at - now) > MAX_ENTRY_OFFSET or (captured and abs(now - captured) > MAX_POSITION_AGE):
        return "offset"
    if position["accuracy_m"] > MAX_ACCURACY_M:
        return "inaccurate"
    return "ok"


def nearest(position: dict, spots: list[dict]) -> tuple[dict | None, float | None]:
    """The closest spot and its distance."""
    best, best_distance = None, None
    for spot in spots:
        distance = distance_m(position["lat"], position["lon"], spot["lat"], spot["lon"])
        if best_distance is None or distance < best_distance:
            best, best_distance = spot, distance
    return best, best_distance


def match(position: dict, spots: list[dict]) -> dict | None:
    """The spot whose radius holds the position (the nearest when several do)."""
    inside = [(distance_m(position["lat"], position["lon"], spot["lat"], spot["lon"]), spot) for spot in spots]
    inside = [(distance, spot) for distance, spot in inside if distance <= spot["radius_m"]]
    return min(inside, key=lambda item: item[0])[1] if inside else None


def suggestion(position: dict | None, at: datetime | None, now: datetime, spots: list[dict]) -> dict:
    """What the entry form preselects: the place of the matching spot, or none with the reason."""
    status = usable(position, at, now)
    if status != "ok":
        return {"status": status, "place_id": None, "hint": STATUS_HINTS[status]}
    spot = match(position, spots)
    if spot is None:
        return {"status": "unknown", "place_id": None, "hint": STATUS_HINTS["unknown"]}
    return {"status": "matched", "place_id": spot["place_id"], "hint": STATUS_HINTS["matched"]}


def clusters(locations: list[dict], spots: list[dict], names: dict[int, str]) -> list[dict]:
    """Positions without a place, grouped greedily (oldest first) by distance to a group's centre."""
    groups: list[dict] = []
    for item in sorted(locations, key=lambda row: row["at"]):
        group = next((group for group in groups
                      if distance_m(item["lat"], item["lon"], group["lat"], group["lon"]) <= CLUSTER_RADIUS_M), None)
        if group is None:
            group = {"lat": item["lat"], "lon": item["lon"], "members": []}
            groups.append(group)
        group["members"].append(item)
        group["lat"] = sum(member["lat"] for member in group["members"]) / len(group["members"])
        group["lon"] = sum(member["lon"] for member in group["members"]) / len(group["members"])
    result = []
    for group in groups:
        lat, lon = round(group["lat"], 6), round(group["lon"], 6)
        near, distance = nearest({"lat": lat, "lon": lon}, spots)
        result.append({
            "lat": lat, "lon": lon, "count": len(group["members"]),
            "first_at": group["members"][0]["at"], "last_at": group["members"][-1]["at"],
            "nearest_place": near and {"id": near["place_id"], "name": names.get(near["place_id"]),
                                       "distance_m": round(distance)},
        })
    return sorted(result, key=lambda group: (-group["count"], group["last_at"]))


def validate_name(value) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    if not name:
        raise ValueError("Bitte einen Namen für den Ort eingeben")
    return name


def validate_spot(value: dict) -> dict:
    """Coordinates and radius of one spot of a place."""
    try:
        radius = int(value.get("radius_m") or DEFAULT_RADIUS_M)
    except (TypeError, ValueError):
        raise ValueError("Radius muss eine ganze Zahl in Metern sein") from None
    if not RADIUS_LIMITS[0] <= radius <= RADIUS_LIMITS[1]:
        raise ValueError(f"Radius muss zwischen {RADIUS_LIMITS[0]} und {RADIUS_LIMITS[1]} m liegen")
    position = parse_position({"lat": value.get("lat"), "lon": value.get("lon")})
    if position is None:
        raise ValueError("Ungültige Koordinaten")
    return {"lat": position["lat"], "lon": position["lon"], "radius_m": radius}
