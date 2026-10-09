"""Tools for a conversational agent (MCP, app/mcp.py): the same use cases as the entry form.

Every rule stays where the app keeps it: the tools only translate agent arguments into the payloads
of entry.preview / entry.save, basal and the diary, and trim the answers to what a conversation needs.
Nothing here computes a dose or a factor of its own.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app import episodes
from app.common import parse_instant
from app.ingestion import nightscout
from app.services import basal, day_view, diary, entry, read_api, sync
from app.store import config

CERTAINTIES = list(episodes.CERTAINTIES)
SPORT_KINDS = list(episodes.SPORT_LABELS)
READ_TABLES = ["meals", "window-events", "entries", "treatments", "basal", "data", "profile"]
# The dose fields a conversation needs; the rest of dose_help's answer is intermediate values.
DOSE_KEYS = (
    "message", "reason", "options", "carbs_g", "glucose_mgdl", "glucose_at", "glucose_fresh",
    "isf_mgdl_per_unit", "target_mgdl", "cautious_target_mgdl", "forecast_mgdl", "cautious_forecast_mgdl",
    "activity_change_mgdl", "recent_rapid_doses", "auto_meal", "auto_method", "auto_factor_count",
    "auto_factor_min_meals", "auto_surcharge_units", "day_type", "day_type_source", "split_plan")


class ToolError(ValueError):
    """A wrong argument; the agent gets the message and can ask again."""


def _zone() -> ZoneInfo:
    _, settings = config.get_settings()
    return ZoneInfo(settings["timezone"])


def _at(args: dict) -> datetime:
    """The entry time: now unless the user named another time on purpose."""
    raw = args.get("at")
    if raw in (None, ""):
        return datetime.now(timezone.utc).replace(microsecond=0)
    try:
        return parse_instant(str(raw))
    except ValueError:
        raise ToolError("Zeit als ISO-8601 mit Zeitzone angeben, z. B. 2026-10-08T12:30:00+02:00") from None


def _number(args: dict, key: str, low: Decimal, high: Decimal) -> Decimal | None:
    raw = args.get(key)
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ToolError(f"{key} muss eine Zahl sein") from None
    if not value.is_finite() or not low <= value <= high:
        raise ToolError(f"{key} muss zwischen {low} und {high} liegen")
    return value


def _bool(args: dict, key: str) -> bool | None:
    value = args.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise ToolError(f"{key} muss true oder false sein")


def _choice(args: dict, key: str, allowed: list[str]) -> str | None:
    value = args.get(key)
    if value in (None, ""):
        return None
    if value not in allowed:
        raise ToolError(f"{key} muss einer von {', '.join(allowed)} sein")
    return value


def _event_id(args: dict) -> str:
    try:
        return str(uuid.UUID(str(args.get("event_id") or uuid.uuid4())))
    except ValueError:
        raise ToolError("event_id muss eine UUID sein") from None


def _day(args: dict) -> str:
    raw = args.get("day")
    if raw in (None, ""):
        return datetime.now(_zone()).date().isoformat()
    try:
        return date.fromisoformat(str(raw)).isoformat()
    except ValueError:
        raise ToolError("day als JJJJ-MM-TT angeben") from None


def _local(value) -> str | None:
    """Instants in local time, so the agent reads them the way the user speaks."""
    if not value:
        return None
    instant = parse_instant(value) if isinstance(value, str) else value
    return instant.astimezone(_zone()).isoformat()


def _dose_view(dose: dict) -> dict:
    """The suggestion as the entry form shows it: options plus the factor they use."""
    view = {key: dose[key] for key in DOSE_KEYS if key in dose}
    if dose.get("options") is not None:
        auto = dose.get("auto_units_per_ke")
        carbs = Decimal(str(dose.get("carbs_g") or 0))
        # Same choice as the entry form: the calculated factor whenever it exists.
        view["factor"] = ({"units_per_ke": auto, "source": "auto"} if carbs > 0 and auto is not None else
                          {"units_per_ke": dose.get("units_per_ke"), "source": dose.get("factor_source")})
    return view


def _entries(current: dict) -> list[dict]:
    keep = ("id", "at", "kind", "carbs", "insulin", "basal_units", "duration_minutes", "sport_kind", "notes",
            "carbs_certainty", "fat_protein", "glucose_mgdl", "meal_id", "nightscout_synced")
    return [{key: (_local(item[key]) if key == "at" else item[key]) for key in keep if key in item}
            for item in current["timeline"]]


def _meals(day: str) -> list[dict]:
    """The day's main meals with their factor, the same rows as the read API."""
    rows = read_api.api_read("/api/v1/meals", urlencode({"from": day, "to": day}))["rows"]
    keep = ("meal_id", "meal", "at", "day_type", "window_start", "window_end", "end_reason", "excluded",
            "carbs_meal_g", "carbs_total_g", "rapid_units", "g0_mgdl", "g_end_mgdl", "delta_mgdl",
            "sport_effect_mgdl", "factor_ie_per_ke", "blocked_reason", "carbs_certainty", "fat_protein")
    return [{key: row.get(key) for key in keep} for row in rows]


def get_status(args: dict) -> dict:
    """Current BG, what still acts, open split part, today's entries and a correction suggestion."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    today = datetime.now(_zone()).date().isoformat()
    try:
        glucose = sync.current_glucose()
    except (ValueError, nightscout.NightscoutError) as error:
        glucose = {"error": str(error)}
    preview = entry.preview({"day": today, "mode": "meal", "at": now.isoformat(), "carbs": "0"})
    current = day_view.day_payload(today, refresh=False)
    return {
        "now": _local(now), "today": today, "day_type": current.get("day_type"),
        "glucose": glucose, "on_board": preview["on_board"],
        "correction": _dose_view(preview["dose"]),
        "basal_today": [{**dose, "at": _local(dose["at"])} for dose in current["basal"]],
        "basal_suggestion_units": preview["basal_units"],
        "entries_today": _entries(current), "meals_today": _meals(today),
    }


def _form(args: dict, at: datetime) -> dict:
    """Shared arguments of preview and save in the entry form's payload."""
    _, settings = config.get_settings()
    carbs = _number(args, "carbs_g", Decimal(0), Decimal(500)) or Decimal(0)
    change = _number(args, "sport_change_mgdl", Decimal(-500), Decimal(500))
    own_meal, split = _bool(args, "own_main_meal"), _bool(args, "split")
    return {
        "day": at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat(), "at": at.isoformat(),
        "mode": "meal", "carbs": str(carbs), "as": "meal" if own_meal else None, "split": split,
        "consider_sport": change is not None, "sport_effect_mgdl": str(change or 0),
    }


def preview_entry(args: dict) -> dict:
    """What the entry form shows for these carbs: snack or main meal, sport, basal, split, dose options."""
    at = _at(args)
    result = entry.preview(_form(args, at))
    assignment = result["assignment"]
    return {
        "at": _local(at), "day": result["day"],
        "assignment": assignment and {**assignment, "at": _local(assignment["at"])},
        "sport": result["sport"], "basal_suggestion_units": result["basal_units"],
        "day_type": result["day_type"], "split": result["split"],
        "dose": _dose_view(result["dose"]), "on_board": result["on_board"],
    }


def save_entry(args: dict) -> dict:
    """Save a meal, snack, correction or sport entry exactly as the entry form does."""
    kind = _choice(args, "kind", ["meal", "exercise"]) or "meal"
    at = _at(args)
    event_id = _event_id(args)
    payload = _form(args, at)
    insulin = _number(args, "insulin_units", Decimal(0), Decimal(100)) or Decimal(0)
    certainty = _choice(args, "carbs_certainty", CERTAINTIES)
    with_basal = _bool(args, "with_basal")
    payload.update({
        "mode": kind, "insulin": str(insulin), "carbs_certainty": certainty,
        "fat_protein": _bool(args, "fat_protein") is True, "with_basal": with_basal is True,
        "dose_variant": _choice(args, "dose_variant", list(entry.DOSE_VARIANTS)),
        "notes": str(args.get("notes") or "")[:500], "event_id": event_id,
        # A retry with the same event_id must not add a second basal dose either.
        "basal_event_id": str(uuid.uuid5(uuid.UUID(event_id), "basal")),
    })
    if kind == "exercise":
        sport_kind = _choice(args, "sport_kind", SPORT_KINDS)
        minutes = _number(args, "duration_minutes", Decimal(1), Decimal(1440))
        if sport_kind is None or minutes is None or minutes % 1:
            raise ToolError("Sport braucht sport_kind und duration_minutes (ganze Minuten)")
        payload.update({"sport_kind": sport_kind, "duration_minutes": int(minutes), "consider_sport": False})
    saved = entry.save(payload)
    current = saved["day_view"]
    _, settings = config.get_settings()
    split_due = (at + timedelta(minutes=int(settings.get("split_delay_minutes", 120)))
                 if saved["split_units"] else None)
    return {
        "saved": True, "day": saved["day"], "kind": saved["kind"], "event_id": saved["event_id"],
        "id": saved["id"], "carbs_g": saved["carbs"], "insulin_units": saved["insulin"],
        "split_second_part_units": saved["split_units"], "split_due_at": _local(split_due),
        "basal": saved["basal"],
        "basal_error": saved["basal_error"], "sport_marked": saved["sport_marked"],
        "sport_error": saved["sport_error"],
        "entry": next((item for item in _entries(current) if item["id"] == saved["id"]), None),
    }


def save_basal(args: dict) -> dict:
    units = _number(args, "units", Decimal("0.5"), Decimal(100))
    if units is None:
        raise ToolError("units fehlt")
    at = _at(args)
    result = basal.save_basal({"at": at.isoformat(), "units": str(units), "event_id": _event_id(args),
                               "notes": str(args.get("notes") or "")[:500]})
    return {"saved": True, "at": _local(at), "units": str(units), **result}


def delete_entry(args: dict) -> dict:
    """Remove one entry of a day (Nightscout treatment or local basal) by the id get_day lists."""
    entry_id = args.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        raise ToolError("id fehlt")
    day = _day(args)
    current = day_view.day_payload(day, refresh=False)
    item = next((row for row in current["timeline"] if row["id"] == entry_id), None)
    if item is None:
        raise ToolError("Eintrag an diesem Tag nicht gefunden; id und day aus get_day verwenden")
    if item["kind"] == "basal":
        basal.delete_basal_entry({"id": entry_id})
    else:
        diary.delete_treatment({"id": entry_id, "day": day})
    return {"deleted": True, "day": day, "entry": _entries({"timeline": [item]})[0]}


def get_day(args: dict) -> dict:
    day = _day(args)
    today = datetime.now(_zone()).date().isoformat()
    current = day_view.day_payload(day, refresh=day == today)
    readings = [Decimal(str(row["sgv"])) for row in current["entries"]
                if row.get("sgv") is not None and _local(row["at"])[:10] == day]
    return {
        "day": day, "day_type": current.get("day_type"), "day_type_source": current.get("day_type_source"),
        "glucose": readings and {"count": len(readings), "min_mgdl": str(min(readings)),
                                 "max_mgdl": str(max(readings)),
                                 "mean_mgdl": str(round(sum(readings) / len(readings)))},
        "entries": _entries(current), "meals": _meals(day),
        "sync_error": current.get("sync_error"),
    }


def read_data(args: dict) -> dict:
    """The read API (/api/v1) for longer periods and raw CGM values."""
    table = _choice(args, "table", READ_TABLES) or "meals"
    query = {key: str(args[key]) for key in ("days", "from", "to") if args.get(key) not in (None, "")}
    try:
        return read_api.api_read(f"/api/v1/{table}", urlencode(query))
    except read_api.ApiNotFound:
        raise ToolError("Unbekannte Tabelle") from None


AT = {"type": "string", "description": "Zeitpunkt ISO-8601 mit Zeitzone. Weglassen = jetzt (Standard). "
                                       "Nur setzen, wenn Philipp ausdrücklich eine andere Zeit nennt."}
FORM = {
    "carbs_g": {"type": "number", "minimum": 0, "maximum": 500,
                "description": "Kohlenhydrate in Gramm (1 KE = 10 g). 0 oder weglassen = Korrektur ohne Essen."},
    "at": AT,
    "own_main_meal": {"type": "boolean",
                      "description": "true, wenn ein Eintrag, den die App als Snack einer Mahlzeit einordnet, "
                                     "eine eigene Hauptmahlzeit sein soll."},
    "split": {"type": "boolean",
              "description": "Split-Bolus an/aus. Weglassen = App-Vorgabe (ab split_min_grams KH an)."},
    "sport_change_mgdl": {"type": "number", "minimum": -500, "maximum": 500,
                          "description": "Nur wenn die Vorschau Sport vor der Mahlzeit meldet und Philipp den "
                                         "Einfluss bestätigt: geschätzte BZ-Änderung, −40 senkt, +40 hebt."},
}

TOOLS = [
    {
        "name": "get_status",
        "title": "Wie läuft der Blutzucker?",
        "description": "Aktueller CGM-Wert mit Trend, was noch wirkt (Insulin, KH, Aufschlag; erwarteter Wert "
                       "wenn alles gewirkt hat), offener zweiter Split-Teil, Korrekturvorschlag ohne Essen, "
                       "Basal heute und alle Einträge und Mahlzeitfaktoren von heute.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": get_status,
    },
    {
        "name": "preview_entry",
        "title": "Empfehlung berechnen",
        "description": "Vor jedem Eintrag aufrufen: zeigt, was das Eintragsformular für diese KH zeigt. "
                       "assignment = Snack einer Hauptmahlzeit (snack=true) oder eigene Mahlzeit; sport = Sport "
                       "vor der Mahlzeit gefunden (Einfluss erfragen); basal_suggestion_units > 0 = Basal heute "
                       "noch offen (fragen, ob mit eintragen); split = Split-Bolus vorgeschlagen; dose.options = "
                       "normal / cautious (vorsichtig) / carbs (nur KH), bei Split units = jetzt und "
                       "later_units/later_at = zweiter Teil; ohne KH ggf. Option split = geplanter 2. Teil. "
                       "Speichert nichts.",
        "inputSchema": {"type": "object", "properties": FORM, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": preview_entry,
    },
    {
        "name": "save_entry",
        "title": "Eintrag speichern",
        "description": "Speichert Mahlzeit, Snack, Korrektur oder Sport in Nightscout, genau wie das "
                       "Eintragsformular. Nur aufrufen, wenn Philipp die Werte ausdrücklich bestätigt hat "
                       "(gegessene KH, tatsächlich gespritzte IE). Bei KH immer KH-Sicherheit und fett-/"
                       "eiweißreich mitgeben (erfragen, falls nicht genannt). Bei Split insulin_units = jetzt "
                       "gespritzter Teil, der Rest wird als geplanter 2. Teil vermerkt.",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["meal", "exercise"],
                     "description": "meal = Essen und/oder Insulin (Standard), exercise = Sport."},
            **FORM,
            "insulin_units": {"type": "number", "minimum": 0, "maximum": 100,
                              "description": "Tatsächlich gespritzte schnelle IE (Fiasp)."},
            "carbs_certainty": {"type": "string", "enum": CERTAINTIES,
                                "description": "weighed = gewogen, known = halbwegs sicher (bekanntes Gericht, "
                                               "FDDB-KI), unsure = keine ernsthafte Schätzung."},
            "fat_protein": {"type": "boolean", "description": "true = fett-/eiweißreich."},
            "dose_variant": {"type": "string", "enum": list(entry.DOSE_VARIANTS),
                             "description": "Welcher Vorschlag gewählt wurde (bestimmt beim Split den 2. Teil)."},
            "with_basal": {"type": "boolean",
                           "description": "Den vorgeschlagenen Basal (basal_suggestion_units) mit eintragen."},
            "sport_kind": {"type": "string", "enum": SPORT_KINDS, "description": "Nur für kind=exercise."},
            "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440,
                                 "description": "Nur für kind=exercise."},
            "notes": {"type": "string", "maxLength": 500, "description": "Notiz, z. B. was gegessen wurde."},
            "event_id": {"type": "string", "description": "UUID. Bei Wiederholung nach Fehler/Timeout dieselbe "
                                                          "event_id verwenden, dann entsteht kein Duplikat."},
        }, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        "handler": save_entry,
    },
    {
        "name": "save_basal",
        "title": "Basal speichern",
        "description": "Speichert eine Basal-Injektion (lokal und als Notiz in Nightscout). Nur auf Philipps "
                       "ausdrückliche Angabe.",
        "inputSchema": {"type": "object", "properties": {
            "units": {"type": "number", "minimum": 0.5, "maximum": 100, "description": "IE in 0,5er-Schritten."},
            "at": AT, "notes": {"type": "string", "maxLength": 500},
            "event_id": {"type": "string", "description": "UUID für sichere Wiederholung."},
        }, "required": ["units"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        "handler": save_basal,
    },
    {
        "name": "delete_entry",
        "title": "Eintrag löschen",
        "description": "Löscht einen Eintrag (auch Basal) eines Tages, z. B. einen falsch verstandenen. Nur auf "
                       "ausdrückliche Bitte und nach Nennung des Eintrags. id und day aus get_day/get_status.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string"}, "day": {"type": "string", "description": "JJJJ-MM-TT, Standard heute."},
        }, "required": ["id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                        "openWorldHint": False},
        "handler": delete_entry,
    },
    {
        "name": "get_day",
        "title": "Tag anzeigen",
        "description": "Ein Tagebuchtag: alle Einträge (mit id), Mahlzeiten mit Faktor (IE/KE) oder "
                       "Blockgrund, Tagesart und BZ-Kennzahlen.",
        "inputSchema": {"type": "object", "properties": {
            "day": {"type": "string", "description": "JJJJ-MM-TT, Standard heute."},
        }, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": get_day,
    },
    {
        "name": "read_data",
        "title": "Daten lesen",
        "description": "Lese-API für längere Zeiträume: meals (Faktoren), window-events, entries (CGM), "
                       "treatments, basal, data (alles), profile (Einstellungen und Nightscout-Profil).",
        "inputSchema": {"type": "object", "properties": {
            "table": {"type": "string", "enum": READ_TABLES},
            "days": {"type": "integer", "minimum": 1, "maximum": read_api.API_MAX_DAYS,
                     "description": "Letzte N Tage (Standard 30)."},
            "from": {"type": "string", "description": "JJJJ-MM-TT"}, "to": {"type": "string", "description": "JJJJ-MM-TT"},
        }, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": read_data,
    },
]
