"""Normalize Nightscout records and associate app-entered meal episodes."""
from __future__ import annotations

from app import core
import re
from bisect import bisect_left
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from app.common import local_zone
from app.i18n import tr


TAG = re.compile(r"\[DA1:([a-f0-9-]{36}):(breakfast|lunch|dinner):(meal|meal-dose|correction|snack)\]")
EVENT_TAG = re.compile(r"\[(?:DAE:[a-f0-9-]{36}|DA2:(?:snack|meal|basal)|DASP:\d+(?:\.\d+)?@\d+|DAZ:\d+(?:\.\d+)?@\d+)\]")
# A split bolus on a main meal: the second part's planned IE and its delay in minutes after the meal.
SPLIT_TAG = re.compile(r"\[DASP:(\d+(?:\.\d+)?)@(\d+)\]")
# A main meal dosed with the calculated fat/protein surcharge (app/fpe.py): its mg/dL and how many minutes it acts.
SURCHARGE_TAG = re.compile(r"\[DAZ:(\d+(?:\.\d+)?)@(\d+)\]")
BASAL_MARKER = "[DA2:basal]"
SNACK_GAP = timedelta(minutes=120)
# A carb entry below this that would start a main meal defaults to a snack of no meal;
# from this size a snack of no meal carries the main-meal surcharge.
SMALL_SNACK_GRAMS = Decimal(20)
SPORT_TAG = re.compile(r"\[DAS1:([a-z][a-z0-9_]{0,31})\]")
# Data-quality marks on carb entries: how certain the carbs are (weighed, known dish or FDDB
# estimate, no serious estimate) and the meal factors: fat-/protein-rich, alcohol, fast-acting
#. Alcohol and fast are recorded only.
QUALITY_TAG = re.compile(r"\[DAQ:(weighed|known|unsure|fat|alcohol|fast)\]")
QUALITY_MARKERS = {"fat_protein": "[DAQ:fat]", "alcohol": "[DAQ:alcohol]", "fast": "[DAQ:fast]"}
# Most to least certain; a meal takes the least certain level of its marked carb entries.
CERTAINTIES = ("weighed", "known", "unsure")
CERTAINTY_TAG = re.compile(r"\[DAQ:(weighed|known|unsure)\]")
DAE_TAG = re.compile(r"\[DAE:([a-f0-9-]{36})\]")
# Since 2026-10-26 the LibreLinkUp uploader also sends LibreLinkUp's smoothed history: about every 5 min a
# second reading 1-9 s after a live one, without a trend arrow, uploaded ~17 min later and up to 22 mg/dL off
#. Such a
# reading next to a live one is dropped; on its own (live reading missing) it still fills the gap.
HISTORY_SECONDS = 10


def event_id(item: dict) -> str | None:
    """The app's own id of a Nightscout treatment (its [DAE:…] note marker), stable across edits."""
    match = DAE_TAG.search(str(item.get("notes") or ""))
    return match.group(1) if match else None


def carbs_certainty(notes: str) -> str | None:
    match = CERTAINTY_TAG.search(notes)
    return match.group(1) if match else None


def split_plan(notes: str) -> dict | None:
    match = SPLIT_TAG.search(notes)
    return {"units": match.group(1), "delay_minutes": int(match.group(2))} if match else None


def split_tag(units, delay_minutes: int) -> str:
    return f"[DASP:{units}@{int(delay_minutes)}]"


def surcharge_plan(notes: str) -> dict | None:
    match = SURCHARGE_TAG.search(notes)
    return {"mgdl": match.group(1), "minutes": int(match.group(2))} if match else None


def surcharge_tag(mgdl, minutes: int) -> str:
    return f"[DAZ:{mgdl}@{int(minutes)}]"


def least_certain(levels) -> str | None:
    marked = [level for level in levels if level in CERTAINTIES]
    return max(marked, key=CERTAINTIES.index) if marked else None


SPORT_LABELS = {"jogging": "Joggen", "gym": "Gym", "light_cardio": "Leichtes Cardio", "racing": "Rennsport"}
# The same in the configured language (app/i18n.py): shown and written into new notes. Reading a note strips
# either spelling, since notes written before a language switch keep theirs.
SPORT_NAMES = {kind: tr(label) for kind, label in SPORT_LABELS.items()}
NOTE_LABELS = tuple(dict.fromkeys([*SPORT_LABELS.values(), *SPORT_NAMES.values()]))


def sport_names(settings: dict | None) -> dict[str, str]:
    """Every sport kind with its name: the built-in kinds in the configured language, then the user's own
    kinds (setting `sport_custom`, key -> name), hidden ones included so older sessions keep their name."""
    return {**SPORT_NAMES, **((settings or {}).get("sport_custom") or {})}


def note_labels(settings: dict | None) -> tuple[str, ...]:
    """The labels a sport note may start with: built-in kinds in either language and the user's own names."""
    return tuple(dict.fromkeys([*NOTE_LABELS, *((settings or {}).get("sport_custom") or {}).values()]))


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
        raise ValueError(tr("Ungültiger Ereignistyp"))
    if not re.fullmatch(r"[a-f0-9-]{36}", episode_id):
        raise ValueError(tr("Ungültige Mahlzeit-ID"))
    return f"[DA1:{episode_id}:{meal}:{role}]"


def normalize_entries(raw: list[dict]) -> list[dict]:
    return core.call("episodes.normalize_entries", raw=raw)


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
    return core.call("episodes.normalize_treatments", raw=raw)


def group_diary_followups(treatments: list[dict], settings: dict) -> None:
    """Group later diary carb entries as snacks of the preceding main meal.

    A diary entry becomes a snack when it starts less than 2 h after the
    previous main meal, or inside the analysis window of a main meal of the
    same clock-based meal kind. A diary entry whose meal kind already has a
    main meal that day becomes a snack of no meal, so each kind appears at most
    once per day. Imported Nightscout meals, explicitly tagged roots and
    entries the user marked as main meal ([DA2:meal]) of a free kind remain
    distinct, so a second main meal can still block an overlapping analysis.
    A diary snack stored as such ([DA2:snack]) whose meal became a snack of
    no meal this way is classified afresh, so it can become the main meal.
    """
    core.update_in_place(treatments, core.call("episodes.group_diary_followups", treatments=treatments,
                                               settings=settings))


def classify_diary_entry(item: dict, treatments: list[dict], settings: dict) -> dict | None:
    """Reclassify one diary main meal in place; return the meal it joins as snack.

    A snack of no meal (its kind is taken, no meal precedes it closely) returns that
    taken meal marked `free`."""
    if item.get("eventType") != "Meal Bolus" or float(item.get("carbs") or 0) <= 0:
        return None
    notes = item.get("notes") or ""
    if not EVENT_TAG.search(notes) or item.get("episode_id"):
        return None
    others = [other for other in treatments if other is not item]
    regular = None if "[DA2:meal]" in notes else preceding_meal(item["at"], others, settings)
    taken = taken_meal(item["at"], others, settings)
    root = regular or (taken and {**taken, "free": True})
    if root is None:
        return None
    item["eventType"] = "Snack Bolus" if float(item.get("insulin") or 0) > 0 else "Carb Correction"
    item["notes"] = (notes + " [DA2:snack]").strip()
    return root


def meal_kind_at(at: datetime, settings: dict) -> tuple:
    return core.call("episodes.meal_kind_at", at=at, settings=settings)


def preceding_meal(at: datetime, treatments: list[dict], settings: dict) -> dict | None:
    """Return the main meal a new carb entry at `at` would count as a snack for."""
    return core.call("episodes.preceding_meal", at=at, treatments=treatments, settings=settings)


def taken_meal(at: datetime, treatments: list[dict], settings: dict) -> dict | None:
    """An earlier main meal of the same day and clock-based kind as `at`; each kind counts once a day."""
    return core.call("episodes.taken_meal", at=at, treatments=treatments, settings=settings)


def meals_for_day(treatments: list[dict], day: str, timezone_name: str,
                  settings: dict | None = None) -> list[dict]:
    return core.call(
        "episodes.meals_for_day",
        treatments=treatments, day=day, timezone_name=timezone_name, settings=settings)


def serialize_treatment(item: dict) -> dict:
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in item.items()}
