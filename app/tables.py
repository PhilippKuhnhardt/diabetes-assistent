"""Tables of the read API (meals, window events, CGM, treatments, basal) built from stored day payloads."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

MEAL_COLUMNS = [
    "day", "day_type", "meal", "meal_id", "at", "window_start", "window_end", "end_reason",
    "manual_window", "excluded", "sport_status", "sport_effect_mgdl", "g0_mgdl", "g_end_mgdl",
    "delta_mgdl", "carbs_meal_g", "carbs_total_g", "extra_carb_count", "carbs_times_isf",
    "rapid_units", "insulin_effect_mgdl", "meal_isf", "factor_ie_per_ke", "blocked_reason",
    "carbs_certainty", "fat_protein",
]
WINDOW_EVENT_COLUMNS = ["meal_id", "kind", "at", "carbs_g", "units", "isf", "event_type"]
ENTRY_COLUMNS = ["at", "sgv"]
BASAL_COLUMNS = ["id", "at", "units", "nightscout_synced", "notes"]
TREATMENT_COLUMNS = ["id", "at", "eventType", "carbs", "insulin", "basal_units", "duration",
                     "sport_type", "role", "meal", "episode_id", "carbs_certainty", "fat_protein", "notes"]
TABLE_COLUMNS = {"meals": MEAL_COLUMNS, "window_events": WINDOW_EVENT_COLUMNS, "entries": ENTRY_COLUMNS,
                 "treatments": TREATMENT_COLUMNS, "basal": BASAL_COLUMNS}

README = """# Lese-API Diabetes-Tagebuch

Alle Zeiten in lokaler Zeit mit UTC-Versatz (ISO 8601), Dezimaltrenner Punkt. Die Faktoren
entsprechen der App zum Abrufzeitpunkt (aktuelles Nightscout-Profil für alle Tage, siehe
/api/v1/profile).

- meals: eine Zeile je Hauptmahlzeit. factor_ie_per_ke = 10 × (delta_mgdl
  + sport_effect_mgdl + insulin_effect_mgdl) / carbs_times_isf. sport_effect_mgdl ist positiv
  für eine bestätigte BZ-Senkung durch Sport. g_end_mgdl ist der BZ am Endpunkt.
  excluded = manuell ausgeklammert. blocked_reason ist gesetzt, wenn kein Faktor vorliegt.
  carbs_certainty: KH-Sicherheit, die unsicherste Stufe der markierten KH-Einträge im Fenster:
  weighed = gewogen, known = Gericht bekannt oder FDDB-KI-Auswertung, unsure = keine ernsthafte
  Schätzung, leer = nicht angegeben. fat_protein: mindestens ein KH-Eintrag war als
  „fett-/eiweißreich“ markiert.
- window_events: alle KH- und Insulinereignisse, die in das Fenster einer Mahlzeit zählen.
- entries: alle gespiegelten CGM-Werte in mg/dL.
- treatments: alle gespiegelten Nightscout-Behandlungen inklusive Sport (Basal aus der App
  als Notiz mit basal_units).
- basal: Basal-Injektionen aus der App (lokal gespeichert, nightscout_synced = auch in
  Nightscout). Basal zählt nicht in den Mahlzeitenfaktor.
- profile: Einstellungen und Nightscout-Profil (Korrekturfaktor, I:C, Zielbereich).
"""


def _local(value, zone: ZoneInfo) -> str:
    if not value:
        return ""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment.astimezone(zone).isoformat(timespec="seconds")


def collect(first_day: date, last_day: date, day_payload: Callable[[str], dict],
            excluded: set[str], settings: dict) -> dict[str, list[dict]]:
    """Rows of every read API table for first_day to last_day (inclusive)."""
    zone = ZoneInfo(settings["timezone"])
    meals, window_events, entries, treatments, basal = [], [], [], [], []
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
        # A day payload also covers parts of the neighbouring days; keep only the rows of this
        # local day so each reading or treatment appears once across a multi-day range.
        own = payload["day"]
        entries.extend(row for row in ({"at": _local(item["at"], zone), "sgv": item["sgv"]}
                                       for item in payload.get("entries", [])) if row["at"][:10] == own)
        treatments.extend(row for row in ({**item, "at": _local(item["at"], zone)}
                                          for item in payload.get("treatments", [])) if row["at"][:10] == own)
        basal.extend(row for row in ({**item, "at": _local(item["at"], zone)}
                                     for item in payload.get("basal", [])) if row["at"][:10] == own)
        day += timedelta(days=1)
    return {"meals": meals, "window_events": window_events, "entries": entries,
            "treatments": treatments, "basal": basal}


def rows_for(table: str, rows: list[dict]) -> list[dict]:
    """Rows restricted to the documented columns."""
    columns = TABLE_COLUMNS[table]
    return [{key: row.get(key) for key in columns} for row in rows]

