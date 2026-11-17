"""Tables of the read API (meals, window events, CGM, treatments, basal) built from stored day payloads."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from app.common import local_zone

MEAL_COLUMNS = [
    "day", "day_type", "vacation", "meal", "meal_id", "at", "window_start", "window_end", "end_reason",
    "manual_window", "excluded", "sport_status", "sport_effect_mgdl", "g0_mgdl", "g_end_mgdl",
    "delta_mgdl", "carbs_meal_g", "carbs_total_g", "extra_carb_count", "carbs_times_isf",
    "rapid_units", "insulin_effect_mgdl", "meal_isf", "factor_ie_per_ke", "blocked_reason",
    "carbs_certainty", "fat_protein", "alcohol", "fast", "place", "dish",
]
WINDOW_EVENT_COLUMNS = ["meal_id", "kind", "at", "carbs_g", "units", "isf", "event_type", "share"]
ENTRY_COLUMNS = ["at", "sgv"]
BASAL_COLUMNS = ["id", "at", "units", "nightscout_synced", "notes"]
MEAL_RECORD_COLUMNS = [
    "event_id", "at", "dish", "dish_id", "description", "carbs_g", "insulin_units", "kind", "place",
    "carbs_certainty", "fat_protein", "fast", "meal_id", "meal", "own_meal", "meal_carbs_total_g",
    "factor_ie_per_ke", "g0_mgdl", "g_end_mgdl", "delta_mgdl", "excluded", "blocked_reason",
    "agent_carbs_g", "agent_carbs_low_g", "agent_carbs_high_g", "agent_history_adjustment_g", "agent_fat_g",
    "agent_protein_g", "agent_items", "agent_model", "photos",
]
TREATMENT_COLUMNS = ["id", "at", "eventType", "carbs", "insulin", "basal_units", "duration",
                     "sport_type", "role", "meal", "episode_id", "carbs_certainty", "fat_protein", "alcohol", "fast", "place",
                     "dish", "description", "notes"]
TABLE_COLUMNS = {"meals": MEAL_COLUMNS, "window_events": WINDOW_EVENT_COLUMNS, "entries": ENTRY_COLUMNS,
                 "treatments": TREATMENT_COLUMNS, "basal": BASAL_COLUMNS, "meal_records": MEAL_RECORD_COLUMNS}

README = """# Lese-API Diabetes-Tagebuch

Alle Zeiten in lokaler Zeit mit UTC-Versatz (ISO 8601), Dezimaltrenner Punkt. Die Faktoren
entsprechen der App zum Abrufzeitpunkt (aktuelles Nightscout-Profil für alle Tage, siehe
/api/v1/profile).

- meals: eine Zeile je Hauptmahlzeit. factor_ie_per_ke = 10 × (delta_mgdl
  + sport_effect_mgdl + insulin_effect_mgdl) / carbs_times_isf. sport_effect_mgdl ist positiv
  für eine bestätigte BZ-Senkung durch Sport. g_end_mgdl ist der BZ am Endpunkt.
  excluded = manuell ausgeklammert. blocked_reason ist gesetzt, wenn kein Faktor vorliegt.
  carbs_certainty: KH-Sicherheit, die unsicherste Stufe der markierten KH-Einträge im Fenster:
  weighed = gewogen oder genormt, known = alle relevanten Zutaten bekannt, nur das Gewicht geschätzt,
  unsure = unbekannte Zutaten oder sehr unklare Menge (Bedeutung seit 17.11.2026, vorher: known =
  Gericht bekannt oder FDDB-KI, unsure = keine ernsthafte Schätzung), leer = nicht angegeben. fat_protein: mindestens ein KH-Eintrag war als
  „fett-/eiweißreich“ markiert. alcohol: mindestens ein KH-Eintrag war mit „Alkohol“ markiert.
  fast: mindestens ein KH-Eintrag war als „Schnell“ (wirkt schnell) markiert, seit 14.11.2026.
  place: Ort-Chip der Mahlzeit (ihr eigener Eintrag, sonst der erste gezählte KH-Eintrag mit Ort),
  leer = kein Ort. Orte und Standorte liegen nur in der App, nicht in Nightscout.
  vacation: der Tag liegt in einem Urlaub (Schalter in den Einstellungen); die Automatik lässt
  Urlaubstage nach dem Urlaub aus (Einstellung „Urlaub in der Automatik“).
  dish: Gericht der Mahlzeit aus dem Gerichte-Katalog der App (ihr eigener Eintrag, sonst der
  erste gezählte KH-Eintrag mit Gericht), leer = keines. Seit 16.11.2026.
- meal_records: eine Zeile je KH-Eintrag mit Gericht oder Beschreibung (seit 16.11.2026), samt dem
  Ergebnis der Hauptmahlzeit, in deren Fenster er zählt (meal_id, factor_ie_per_ke, g0/g_end/delta).
  own_meal = der Eintrag ist die Hauptmahlzeit selbst; bei false ist er ein Snack oder Nachschlag in
  ihrem Fenster, das Ergebnis gehört dann dem ganzen Fenster (meal_carbs_total_g).
  agent_*: Schätzung des Essens-Agenten, aus der der Eintrag entstand (carbs_g = gespeicherte KH,
  agent_carbs_g mit Spanne low/high = Schätzung; agent_history_adjustment_g = Anteil, den der Agent
  wegen des Verlaufs des Gerichts aufgeschlagen hat; agent_fat_g / agent_protein_g = geschätztes
  Fett und Eiweiß der Mahlzeit in Gramm, seit 17.11.2026, nur gespeichert, nicht in der App gezeigt;
  agent_items = Bestandteile, je mit carbs_g, fat_g, protein_g). photos: Pfade der
  Fotos, abrufbar mit demselben Schlüssel (JPEG).
  /api/v1/dishes (ohne Zeitraum-Tabellen): der Katalog mit Kennzahlen je Gericht, siehe dort.
- window_events: alle KH- und Insulinereignisse, die in das Fenster einer Mahlzeit zählen.
  kind = insulin_prior: schnell wirksame Injektion vor dem Fenster; sie zählt mit share, dem Anteil
  ihrer Wirkung zwischen Beginn und Endpunkt (units × share × isf, in insulin_effect_mgdl enthalten).
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
    zone = local_zone(settings["timezone"])
    meals, window_events, entries, treatments, basal, meal_records = [], [], [], [], [], []
    day = first_day
    while day <= last_day:
        payload = day_payload(day.isoformat())
        for meal in payload.get("meals", []):
            result = meal.get("calculation") or {}
            meals.append({
                "day": payload["day"], "day_type": payload.get("day_type"),
                "vacation": bool(payload.get("vacation")), "meal": meal.get("meal"),
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
                "place": (meal.get("place") or {}).get("name"),
                "dish": (meal.get("dish") or {}).get("name"),
            })
            for item in result.get("carb_events", []):
                window_events.append({"meal_id": meal["id"], "kind": "carbs", "at": _local(item["at"], zone),
                                      "carbs_g": item["carbs"], "isf": item["isf"], "event_type": item.get("kind")})
            for item in result.get("doses", []):
                window_events.append({"meal_id": meal["id"], "kind": "insulin", "at": _local(item["at"], zone),
                                      "units": item["units"], "isf": item["isf"],
                                      "event_type": item.get("event_type") or item.get("role")})
            for item in result.get("prior_doses", []):
                window_events.append({"meal_id": meal["id"], "kind": "insulin_prior", "at": _local(item["at"], zone),
                                      "units": item["units"], "isf": item["isf"], "share": item["share"]})
        # A day payload also covers parts of the neighbouring days; keep only the rows of this
        # local day so each reading or treatment appears once across a multi-day range.
        own = payload["day"]
        places = {item["id"]: item["place"]["name"] for item in payload.get("timeline", []) if item.get("place")}
        described = {item["id"]: item for item in payload.get("timeline", [])
                     if item.get("dish") or item.get("description")}
        by_meal = {meal["id"]: meal for meal in payload.get("meals", [])}
        for item in described.values():
            if _local(item["at"], zone)[:10] != own:
                continue
            meal = by_meal.get(item.get("meal_id")) or {}
            result = meal.get("calculation") or {}
            meal_records.append({
                "event_id": item.get("event_id"), "at": _local(item["at"], zone),
                "dish": (item.get("dish") or {}).get("name"), "dish_id": (item.get("dish") or {}).get("id"),
                "description": item.get("description") or "", "carbs_g": item.get("carbs"),
                "insulin_units": item.get("insulin"), "kind": item.get("kind"),
                "place": (item.get("place") or {}).get("name"),
                "carbs_certainty": item.get("carbs_certainty"), "fat_protein": item.get("fat_protein"),
                "fast": item.get("fast"), "meal_id": meal.get("id"), "meal": meal.get("meal"),
                "own_meal": bool(meal) and meal.get("source_id") == item["id"],
                "meal_carbs_total_g": result.get("carbs_total"),
                "factor_ie_per_ke": result.get("factor_units_per_ke"), "g0_mgdl": result.get("g0"),
                "g_end_mgdl": result.get("g3"), "delta_mgdl": result.get("delta_mgdl"),
                "excluded": meal.get("id") in excluded if meal else None,
                "blocked_reason": (None if result.get("factor_units_per_ke") else result.get("reason")) if meal else None,
            })
        entries.extend(row for row in ({"at": _local(item["at"], zone), "sgv": item["sgv"]}
                                       for item in payload.get("entries", [])) if row["at"][:10] == own)
        treatments.extend(row for row in ({**item, "at": _local(item["at"], zone), "place": places.get(item["id"]),
                                           "dish": ((described.get(item["id"]) or {}).get("dish") or {}).get("name"),
                                           "description": (described.get(item["id"]) or {}).get("description") or ""}
                                          for item in payload.get("treatments", [])) if row["at"][:10] == own)
        basal.extend(row for row in ({**item, "at": _local(item["at"], zone)}
                                     for item in payload.get("basal", [])) if row["at"][:10] == own)
        day += timedelta(days=1)
    return {"meals": meals, "window_events": window_events, "entries": entries,
            "treatments": treatments, "basal": basal, "meal_records": meal_records}


def rows_for(table: str, rows: list[dict]) -> list[dict]:
    """Rows restricted to the documented columns."""
    columns = TABLE_COLUMNS[table]
    return [{key: row.get(key) for key in columns} for row in rows]

