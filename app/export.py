"""Read-only data export as a ZIP of CSV files for offline analysis of the factor model."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

MEAL_COLUMNS = [
    "day", "day_type", "meal", "meal_id", "at", "window_start", "window_end", "end_reason",
    "manual_window", "excluded", "sport_status", "sport_effect_mgdl", "g0_mgdl", "g_end_mgdl",
    "delta_mgdl", "carbs_meal_g", "carbs_total_g", "extra_carb_count", "carbs_times_isf",
    "rapid_units", "insulin_effect_mgdl", "meal_isf", "factor_ie_per_ke", "blocked_reason",
    "carbs_unsure", "fat_protein",
]
WINDOW_EVENT_COLUMNS = ["meal_id", "kind", "at", "carbs_g", "units", "isf", "event_type"]
ENTRY_COLUMNS = ["at", "sgv"]
TREATMENT_COLUMNS = ["id", "at", "eventType", "carbs", "insulin", "basal_units", "duration",
                     "sport_type", "role", "meal", "episode_id", "carbs_unsure", "fat_protein", "notes"]

README = """# Datenexport Diabetes-Tagebuch

Alle Zeiten in lokaler Zeit mit UTC-Versatz (ISO 8601), Dezimaltrenner Punkt. Die Faktoren
entsprechen der App zum Exportzeitpunkt (aktuelles Nightscout-Profil für alle Tage, siehe
profile.json).

- meals.csv: eine Zeile je Hauptmahlzeit. factor_ie_per_ke = 10 × (delta_mgdl
  + sport_effect_mgdl + insulin_effect_mgdl) / carbs_times_isf. sport_effect_mgdl ist positiv
  für eine bestätigte BZ-Senkung durch Sport. g_end_mgdl ist der BZ am Endpunkt.
  excluded = manuell ausgeklammert. blocked_reason ist gesetzt, wenn kein Faktor vorliegt.
  carbs_unsure / fat_protein: mindestens ein KH-Eintrag im Fenster war als „KH unsicher“
  bzw. „fett-/eiweißreich“ markiert.
- window_events.csv: alle KH- und Insulinereignisse, die in das Fenster einer Mahlzeit zählen.
- entries.csv: alle gespiegelten CGM-Werte in mg/dL.
- treatments.csv: alle gespiegelten Nightscout-Behandlungen inklusive Basal und Sport.
- profile.json: Einstellungen und Nightscout-Profil (Korrekturfaktor, I:C, Zielbereich).
"""


def _local(value, zone: ZoneInfo) -> str:
    if not value:
        return ""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment.astimezone(zone).isoformat(timespec="seconds")


def _csv(columns: list[str], rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row[key] for key in columns})
    return buffer.getvalue()


def build_zip(first_day: date, last_day: date, day_payload: Callable[[str], dict],
              excluded: set[str], settings: dict) -> bytes:
    """Collect every day from first_day to last_day (inclusive) into a ZIP of CSV files."""
    zone = ZoneInfo(settings["timezone"])
    meals, window_events, entries, treatments = [], [], [], []
    day = first_day
    while day <= last_day:
        payload = day_payload(day.isoformat())
        for meal in payload.get("meals", []):
            result = meal.get("calculation") or {}
            meals.append({
                "day": payload["day"], "day_type": payload.get("day_type"), "meal": meal.get("meal"),
                "meal_id": meal["id"], "at": _local(meal.get("at"), zone),
                "window_start": _local(result.get("start") or result.get("window_start"), zone),
                "window_end": _local(result.get("end") or result.get("window_end"), zone),
                "end_reason": result.get("end_reason"), "manual_window": result.get("manual_window"),
                "excluded": meal["id"] in excluded, "sport_status": (meal.get("sport") or {}).get("status"),
                "sport_effect_mgdl": result.get("sport_effect_mgdl"),
                "g0_mgdl": result.get("g0"), "g_end_mgdl": result.get("g3"),
                "delta_mgdl": result.get("delta_mgdl"), "carbs_meal_g": result.get("carbs"),
                "carbs_total_g": result.get("carbs_total"), "extra_carb_count": result.get("extra_carb_count"),
                "carbs_times_isf": result.get("carbs_times_isf_total"),
                "rapid_units": result.get("rapid_units_in_window"),
                "insulin_effect_mgdl": result.get("insulin_effect_mgdl"), "meal_isf": result.get("meal_isf"),
                "factor_ie_per_ke": result.get("factor_units_per_ke"),
                "blocked_reason": None if result.get("factor_units_per_ke") else result.get("reason"),
                **(meal.get("quality") or {}),
            })
            for item in result.get("carb_events", []):
                window_events.append({"meal_id": meal["id"], "kind": "carbs", "at": _local(item["at"], zone),
                                      "carbs_g": item["carbs"], "isf": item["isf"], "event_type": item.get("kind")})
            for item in result.get("doses", []):
                window_events.append({"meal_id": meal["id"], "kind": "insulin", "at": _local(item["at"], zone),
                                      "units": item["units"], "isf": item["isf"],
                                      "event_type": item.get("event_type") or item.get("role")})
        entries.extend({"at": _local(item["at"], zone), "sgv": item["sgv"]} for item in payload.get("entries", []))
        treatments.extend({**item, "at": _local(item["at"], zone)} for item in payload.get("treatments", []))
        day += timedelta(days=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("LIESMICH.md", README)
        archive.writestr("meals.csv", _csv(MEAL_COLUMNS, meals))
        archive.writestr("window_events.csv", _csv(WINDOW_EVENT_COLUMNS, window_events))
        archive.writestr("entries.csv", _csv(ENTRY_COLUMNS, entries))
        archive.writestr("treatments.csv", _csv(TREATMENT_COLUMNS, treatments))
        archive.writestr("profile.json", json.dumps(
            {"exported_days": [first_day.isoformat(), last_day.isoformat()], "settings": settings},
            ensure_ascii=False, indent=2, default=str))
    return buffer.getvalue()
