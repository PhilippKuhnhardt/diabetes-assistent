"""Normalize Nightscout records and associate app-entered meal episodes."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TAG = re.compile(r"\[DA1:([a-f0-9-]{36}):(breakfast|lunch|dinner):(meal|meal-dose|correction|snack)\]")
EVENT_TAG = re.compile(r"\[DAE:[a-f0-9-]{36}\]")
SPORT_TAG = re.compile(r"\[DAS1:(jogging|gym|light_cardio)\]")
SPORT_LABELS = {"jogging": "Joggen", "gym": "Gym", "light_cardio": "Leichtes Cardio"}


def parse_at(value: dict) -> datetime:
    if value.get("created_at"):
        return datetime.fromisoformat(value["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    millis = int(value["date"])
    return datetime.fromtimestamp(millis // 1000, timezone.utc) + timedelta(milliseconds=millis % 1000)


def treatment_tag(record: dict) -> dict | None:
    match = TAG.search(str(record.get("notes") or ""))
    if not match:
        return None
    return {"episode_id": match.group(1), "meal": match.group(2), "role": match.group(3)}


def tag(episode_id: str, meal: str, role: str) -> str:
    if meal not in ("breakfast", "lunch", "dinner") or role not in ("meal", "meal-dose", "correction", "snack"):
        raise ValueError("Ungültiger Ereignistyp")
    if not re.fullmatch(r"[a-f0-9-]{36}", episode_id):
        raise ValueError("Ungültige Mahlzeit-ID")
    return f"[DA1:{episode_id}:{meal}:{role}]"


def normalize_entries(raw: list[dict]) -> list[dict]:
    result = []
    for item in raw:
        if item.get("sgv") is None:
            continue
        if item.get("date"):
            millis = int(item["date"])
            at = datetime.fromtimestamp(millis // 1000, timezone.utc) + timedelta(milliseconds=millis % 1000)
        else:
            at = datetime.fromisoformat(item["dateString"].replace("Z", "+00:00")).astimezone(timezone.utc)
        result.append({"id": str(item.get("_id") or item.get("date")), "at": at, "sgv": item["sgv"]})
    return sorted(result, key=lambda x: x["at"])


def normalize_treatments(raw: list[dict]) -> list[dict]:
    result = []
    for item in raw:
        try:
            at = parse_at(item)
        except (ValueError, KeyError):
            continue
        marker = treatment_tag(item) or {}
        sport_notes = str(item.get("notes") or "")
        sport_match = SPORT_TAG.search(sport_notes) if item.get("eventType") == "Exercise" else None
        sport_type = sport_match.group(1) if sport_match else None
        if item.get("eventType") == "Exercise" and not sport_type:
            # Recover recognizable legacy/manual entries from their note prefix.
            for key, label in SPORT_LABELS.items():
                prefix = label.casefold()
                note = sport_notes.strip().casefold()
                if note == prefix or note.startswith((prefix + " —", prefix + " |")):
                    sport_type = key
                    break
        result.append({
            "id": str(item.get("_id") or item.get("identifier") or ""),
            "at": at,
            "eventType": item.get("eventType", ""),
            "carbs": item.get("carbs") or 0,
            "insulin": item.get("insulin") or 0,
            "basal_units": item.get("basal_units") or 0,
            "duration": item.get("duration") or 0,
            "notes": item.get("notes") or "",
            "sport_type": sport_type,
            **marker,
        })
    return sorted(result, key=lambda x: x["at"])


def meals_for_day(treatments: list[dict], day: str, timezone_name: str) -> list[dict]:
    zone = ZoneInfo(timezone_name)
    roots = []
    for item in treatments:
        tagged = item.get("role") == "meal" and item.get("episode_id")
        imported = not item.get("episode_id") and item.get("eventType") == "Meal Bolus" and float(item.get("carbs") or 0) > 0
        if not (tagged or imported):
            continue
        if item["at"].astimezone(zone).date().isoformat() != day:
            continue
        local_hour = item["at"].astimezone(zone).hour
        inferred = "breakfast" if local_hour < 11 else "lunch" if local_hour < 17 else "dinner"
        roots.append({
            "id": item["episode_id"] if tagged else item["id"],
            "source_id": item["id"], "imported": bool(imported),
            "at": item["at"], "meal": item["meal"] if tagged else inferred,
            "carbs": item["carbs"], "notes": EVENT_TAG.sub("", TAG.sub("", item["notes"])).strip(),
        })
    return roots


def serialize_treatment(item: dict) -> dict:
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in item.items()}
