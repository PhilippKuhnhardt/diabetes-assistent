"""Normalize Nightscout records and associate app-entered meal episodes."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TAG = re.compile(r"\[DA1:([a-f0-9-]{36}):(breakfast|lunch|dinner):(meal|meal-dose|correction|snack)\]")
EVENT_TAG = re.compile(r"\[(?:DAE:[a-f0-9-]{36}|DA2:(?:snack|meal|basal)|DASP:\d+(?:\.\d+)?@\d+)\]")
# A split bolus on a main meal: the second part's planned IE and its delay in minutes after the meal.
SPLIT_TAG = re.compile(r"\[DASP:(\d+(?:\.\d+)?)@(\d+)\]")
BASAL_MARKER = "[DA2:basal]"
SNACK_GAP = timedelta(minutes=120)
SPORT_TAG = re.compile(r"\[DAS1:(jogging|gym|light_cardio|racing)\]")
# Data-quality marks on carb entries: how certain the carbs are (weighed, known dish or FDDB
# estimate, no serious estimate) and whether the meal is fat-/protein-rich.
QUALITY_TAG = re.compile(r"\[DAQ:(weighed|known|unsure|fat)\]")
QUALITY_MARKERS = {"fat_protein": "[DAQ:fat]"}
# Most to least certain; a meal takes the least certain level of its marked carb entries.
CERTAINTIES = ("weighed", "known", "unsure")
CERTAINTY_TAG = re.compile(r"\[DAQ:(weighed|known|unsure)\]")


def carbs_certainty(notes: str) -> str | None:
    match = CERTAINTY_TAG.search(notes)
    return match.group(1) if match else None


def split_plan(notes: str) -> dict | None:
    match = SPLIT_TAG.search(notes)
    return {"units": match.group(1), "delay_minutes": int(match.group(2))} if match else None


def split_tag(units, delay_minutes: int) -> str:
    return f"[DASP:{units}@{int(delay_minutes)}]"


def least_certain(levels) -> str | None:
    marked = [level for level in levels if level in CERTAINTIES]
    return max(marked, key=CERTAINTIES.index) if marked else None
SPORT_LABELS = {"jogging": "Joggen", "gym": "Gym", "light_cardio": "Leichtes Cardio", "racing": "Rennsport"}


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


def finger_mgdl(item: dict) -> Decimal | None:
    """A Nightscout BG Check (blood glucose measured by hand) in mg/dL, else None."""
    if item.get("eventType") != "BG Check":
        return None
    try:
        value = Decimal(str(item.get("glucose")))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    if "mmol" in str(item.get("units") or "").lower():
        value = (value * Decimal("18.0182")).quantize(Decimal(1))
    return value


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
            "carbs_certainty": carbs_certainty(str(item.get("notes") or "")),
            **{flag: tag in str(item.get("notes") or "") for flag, tag in QUALITY_MARKERS.items()},
            "split": split_plan(str(item.get("notes") or "")),
            "finger_mgdl": finger_mgdl(item),
            **marker,
        })
    return sorted(result, key=lambda x: x["at"])


def group_diary_followups(treatments: list[dict], settings: dict) -> None:
    """Group later diary carb entries as snacks of the preceding main meal.

    A diary entry becomes a snack when it starts less than 2 h after the
    previous main meal, or inside the analysis window of a main meal of the
    same clock-based meal kind. A diary entry whose meal kind already has a
    main meal that day becomes a snack of no meal, so each kind appears at most
    once per day. Imported Nightscout meals, explicitly tagged roots and
    entries the user marked as main meal ([DA2:meal]) of a free kind remain
    distinct, so a second main meal can still block an overlapping analysis.
    """
    for item in treatments:
        classify_diary_entry(item, treatments, settings)


def classify_diary_entry(item: dict, treatments: list[dict], settings: dict) -> dict | None:
    """Reclassify one diary main meal in place; return the meal it joins as snack."""
    if item.get("eventType") != "Meal Bolus" or float(item.get("carbs") or 0) <= 0:
        return None
    notes = item.get("notes") or ""
    if not EVENT_TAG.search(notes) or item.get("episode_id"):
        return None
    others = [other for other in treatments if other is not item]
    root = taken_meal(item["at"], others, settings)
    if root is None and "[DA2:meal]" not in notes:
        root = preceding_meal(item["at"], others, settings)
    if root is None:
        return None
    item["eventType"] = "Snack Bolus" if float(item.get("insulin") or 0) > 0 else "Carb Correction"
    item["notes"] = (notes + " [DA2:snack]").strip()
    return root


def meal_kind_at(at: datetime, settings: dict) -> tuple:
    local = at.astimezone(ZoneInfo(settings["timezone"]))
    minute = local.hour * 60 + local.minute
    breakfast_end = int(settings.get("breakfast_end_minute", 660))
    lunch_end = int(settings.get("lunch_end_minute", 1020))
    return local.date(), "breakfast" if minute < breakfast_end else "lunch" if minute < lunch_end else "dinner"


def preceding_meal(at: datetime, treatments: list[dict], settings: dict) -> dict | None:
    """Return the main meal a new carb entry at `at` would count as a snack for."""
    window = timedelta(minutes=int(settings.get("window_minutes", 180)))
    kind = meal_kind_at(at, settings)
    root = None
    for item in sorted(treatments, key=lambda x: x["at"]):
        if item["at"] > at:
            break
        if item.get("eventType") == "Meal Bolus" and float(item.get("carbs") or 0) > 0:
            root = {"id": item.get("id"), "at": item["at"], "end": item["at"] + window,
                    "meal": item.get("meal") or meal_kind_at(item["at"], settings)[1],
                    "same_kind": meal_kind_at(item["at"], settings) == kind}
        elif (root is not None and item["at"] < root["end"]
                and float(item.get("insulin") or 0) > 0
                and item.get("eventType") in {"Snack Bolus", "Correction Bolus", "Bolus"}):
            root["end"] = max(root["end"], item["at"] + window)
    if root is None or root["at"] == at:
        return None
    if at - root["at"] < SNACK_GAP or (root["same_kind"] and at < root["end"]):
        return root
    return None


def taken_meal(at: datetime, treatments: list[dict], settings: dict) -> dict | None:
    """An earlier main meal of the same day and clock-based kind as `at`; each kind counts once a day."""
    kind = meal_kind_at(at, settings)
    for item in sorted(treatments, key=lambda x: x["at"]):
        if item["at"] >= at:
            break
        if (item.get("eventType") == "Meal Bolus" and float(item.get("carbs") or 0) > 0
                and meal_kind_at(item["at"], settings) == kind):
            return {"id": item.get("id"), "at": item["at"], "meal": item.get("meal") or kind[1]}
    return None


def meals_for_day(treatments: list[dict], day: str, timezone_name: str,
                  settings: dict | None = None) -> list[dict]:
    zone = ZoneInfo(timezone_name)
    roots = []
    settings = settings or {}
    breakfast_end = int(settings.get("breakfast_end_minute", 660))
    lunch_end = int(settings.get("lunch_end_minute", 1020))
    for item in treatments:
        tagged = item.get("role") == "meal" and item.get("episode_id")
        imported = not item.get("episode_id") and item.get("eventType") == "Meal Bolus" and float(item.get("carbs") or 0) > 0
        if not (tagged or imported):
            continue
        if item["at"].astimezone(zone).date().isoformat() != day:
            continue
        local = item["at"].astimezone(zone)
        minute = local.hour * 60 + local.minute
        inferred = ("breakfast" if minute < breakfast_end else
                    "lunch" if minute < lunch_end else "dinner")
        roots.append({
            "id": item["episode_id"] if tagged else item["id"],
            "source_id": item["id"], "imported": bool(imported),
            "at": item["at"], "meal": item["meal"] if tagged else inferred,
            "carbs": item["carbs"], "notes": QUALITY_TAG.sub("", EVENT_TAG.sub("", TAG.sub("", item["notes"]))).strip(),
        })
    return roots


def serialize_treatment(item: dict) -> dict:
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in item.items()}
