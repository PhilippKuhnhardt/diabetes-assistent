"""Tools for a conversational agent (MCP, app/mcp.py): the same use cases as the entry form.

Every rule stays where the app keeps it: the tools only translate agent arguments into the payloads
of entry.preview / entry.save, basal and the diary, and trim the answers to what a conversation needs.
Nothing here computes a dose or a factor of its own.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from annotated_types import Ge, Le
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.json_schema import GenerateJsonSchema

from app import episodes
from app.common import local_zone, parse_instant
from app.ingestion import nightscout
from app.services import basal, day_view, diary, dishes, entry, read_api, sync
from app.store import config, places as place_store
from app.i18n import tr

CERTAINTIES = list(episodes.CERTAINTIES)
SPORT_KINDS = list(episodes.SPORT_LABELS)
READ_TABLES = ["meals", "window-events", "entries", "treatments", "basal", "meal-records", "data", "profile"]
# The dose fields a conversation needs; the rest of dose_help's answer is intermediate values.
DOSE_KEYS = (
    "message", "reason", "options", "carbs_g", "glucose_mgdl", "glucose_at", "glucose_fresh", "glucose_source",
    "isf_mgdl_per_unit", "target_mgdl", "cautious_target_mgdl", "low_target_mgdl", "low_note", "forecast_mgdl", "cautious_forecast_mgdl",
    "recent_rapid_doses", "auto_meal", "auto_method", "auto_factor_count",
    "auto_factor_min_meals", "auto_surcharge_units", "surcharge_choice", "day_type", "day_type_source", "split_plan", "slider")


class ToolError(ValueError):
    """A wrong argument; the agent gets the message and can ask again."""


# Tool arguments: one model per tool validates the agent's arguments and yields the inputSchema it reads.
class Args(BaseModel):
    # Unknown arguments are ignored, as before; the schema still tells the agent there are none.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_is_missing(cls, data):
        """An empty string or null means "not given", like a field the agent left out."""
        return {key: value for key, value in data.items() if value is not None and value != ""} \
            if isinstance(data, dict) else data


AT_TEXT = (tr("Zeitpunkt ISO-8601 mit Zeitzone. Weglassen = jetzt (Standard). "
           "Nur setzen, wenn Philipp ausdrücklich eine andere Zeit nennt."))
DAY_TEXT = tr("JJJJ-MM-TT, Standard heute.")


class GetStatusArgs(Args):
    pass


class EntryArgs(Args):
    carbs_g: Annotated[Decimal | None, Field(ge=0, le=500, description=
        tr("Kohlenhydrate in Gramm (1 KE = 10 g). 0 oder weglassen = Korrektur ohne Essen."))] = None
    at: Annotated[str | None, Field(description=AT_TEXT)] = None
    own_main_meal: Annotated[bool | None, Field(description=
        tr("true, wenn ein Eintrag, den die App als Snack einer Mahlzeit einordnet, eine eigene Hauptmahlzeit sein soll."))] = None
    close_meal: Annotated[bool | None, Field(description=
        tr("true, wenn ein Snack, den die App einer Mahlzeit zuordnet (assignment.close gesetzt), stattdessen die "
        "Mahlzeit abschließen soll: ihr Zeitraum endet beim Snack, der Snack zählt als Snack ohne Mahlzeit."))] = None
    split: Annotated[bool | None, Field(description=
        tr("Split-Bolus an/aus. Weglassen = App-Vorgabe (ab split_min_grams KH an)."))] = None
    finger_mgdl: Annotated[Decimal | None, Field(ge=20, le=600, description=
        tr("Blutig gemessener BZ in mg/dL (z. B. Sensor ausgefallen). Ersetzt für die Empfehlung einen fehlenden oder "
        "älteren CGM-Wert; save_entry speichert ihn als Nightscout-BG-Check, auch ohne KH und Insulin."))] = None
    carbs_certainty: Annotated[Literal[tuple(CERTAINTIES)] | None, Field(description=
        tr("weighed = gewogen oder genormt (Packung, Portionsangabe), known = alle relevanten Zutaten bekannt, nur das Gewicht "
        "geschätzt, unsure = unbekannte Zutaten oder sehr unklare Menge."))] = None
    fat_protein: Annotated[bool | None, Field(description=tr("true = fett-/eiweißreich."))] = None
    fast: Annotated[bool | None, Field(description=
        tr("true = wirkt schnell (z. B. Saft, Weißbrot; nur erfasst, ändert keine Rechnung). Mit fat_protein kombinierbar."))] = None
    fat_g: Annotated[Decimal | None, Field(ge=0, le=500, description=
        tr("Geschätztes Fett der ganzen Mahlzeit in Gramm. Mit protein_g bietet die Empfehlung den berechneten "
        "Fett/Eiweiß-Zuschlag an (surcharge_choice)."))] = None
    protein_g: Annotated[Decimal | None, Field(ge=0, le=500, description=tr("Geschätztes Eiweiß der ganzen Mahlzeit in Gramm."))] = None
    surcharge_mode: Annotated[Literal["static", "fpe"] | None, Field(description=
        tr("Zuschlag der Hauptmahlzeit: static = fest (Mahlzeitzuschlag, mit fat_protein plus Fett-Zuschlag), fpe = aus fat_g "
        "und protein_g berechnet (dann Split: KH jetzt, Zuschlag später). Weglassen = static."))] = None
    place: Annotated[str | None, Field(description=
        tr("Ort-Chip, an dem gegessen wurde (z. B. Kantine, Restaurant), nur bei KH. Die Vorschau listet die vorhandenen "
        "Orte in places; weglassen, wenn Philipp keinen nennt."))] = None


class SaveEntryArgs(EntryArgs):
    kind: Annotated[Literal["meal", "exercise"] | None, Field(description=
        tr("meal = Essen und/oder Insulin (Standard), exercise = Sport."))] = None
    insulin_units: Annotated[Decimal | None, Field(ge=0, le=100, description=
        tr("Tatsächlich gespritzte schnelle IE (Fiasp)."))] = None
    dose_variant: Annotated[Literal[entry.DOSE_VARIANTS] | None, Field(description=
        tr("Welcher Vorschlag gewählt wurde (bestimmt beim Split den 2. Teil)."))] = None
    with_basal: Annotated[bool | None, Field(description=
        tr("Den vorgeschlagenen Basal (basal_suggestion_units) mit eintragen."))] = None
    dish: Annotated[str | None, Field(description=
        tr("Gericht aus dem Katalog (list_dishes), z. B. Porridge, nur bei KH. Ein vorhandenes Gericht mit genau seinem "
        "Namen nennen, damit sein Verlauf zusammenbleibt; ein neuer Name legt ein neues Gericht an."))] = None
    description: Annotated[str | None, Field(description=
        tr("Was gegessen wurde, in Philipps Worten (Bestandteile, Mengen), nur bei KH."))] = None
    sport_kind: Annotated[Literal[tuple(SPORT_KINDS)] | None, Field(description=tr("Nur für kind=exercise."))] = None
    duration_minutes: Annotated[int | None, Field(ge=1, le=1440, description=tr("Nur für kind=exercise."))] = None
    sport_change_mgdl: Annotated[Decimal | None, Field(ge=-500, le=500, description=
        tr("Nur für kind=exercise, wenn Philipp einen eigenen Einfluss nennt: BZ-Änderung in mg/dL, −70 senkt, +40 hebt. "
        "Weglassen = Standard der Sportart. Wirkt automatisch über die Wirkdauer der Sportart nach dem Ende."))] = None
    notes: Annotated[str | None, Field(description=tr("Notiz, z. B. was gegessen wurde."),
                                       json_schema_extra={"maxLength": 500})] = None
    event_id: Annotated[str | None, Field(description=
        tr("UUID. Bei Wiederholung nach Fehler/Timeout dieselbe event_id verwenden, dann entsteht kein Duplikat."))] = None


class SaveBasalArgs(Args):
    units: Annotated[Decimal, Field(ge=Decimal("0.5"), le=100, description=tr("IE in 0,5er-Schritten."))]
    at: Annotated[str | None, Field(description=AT_TEXT)] = None
    notes: Annotated[str | None, Field(json_schema_extra={"maxLength": 500})] = None
    event_id: Annotated[str | None, Field(description=tr("UUID für sichere Wiederholung."))] = None


class DeleteEntryArgs(Args):
    id: str
    day: Annotated[str | None, Field(description=DAY_TEXT)] = None


class GetDayArgs(Args):
    day: Annotated[str | None, Field(description=DAY_TEXT)] = None


class ListDishesArgs(Args):
    dish: Annotated[str | None, Field(description=tr("Name eines Gerichts für dessen Verlauf."))] = None
    days: Annotated[int | None, Field(ge=1, le=dishes.VIEW_MAX_DAYS, description=
        tr("Zeitraum in Tagen (Standard {days}).", days=dishes.VIEW_DAYS))] = None


class ReadDataArgs(Args):
    table: Literal[tuple(READ_TABLES)] | None = None
    days: Annotated[int | None, Field(ge=1, le=read_api.API_MAX_DAYS, description=tr("Letzte N Tage (Standard 30)."))] = None
    from_: Annotated[str | None, Field(alias="from", description="JJJJ-MM-TT")] = None
    to: Annotated[str | None, Field(description="JJJJ-MM-TT")] = None


class _Schema(GenerateJsonSchema):
    """The plain JSON Schema an agent reads: numbers as numbers, no titles, optional fields without null."""

    def decimal_schema(self, schema):
        value = {"type": "number"}
        for key, name in (("ge", "minimum"), ("le", "maximum")):
            if schema.get(key) is not None:
                bound = Decimal(schema[key])
                value[name] = int(bound) if bound == bound.to_integral_value() else float(bound)
        return value

    def nullable_schema(self, schema):
        return self.generate_inner(schema["schema"])

    def default_schema(self, schema):
        return self.generate_inner(schema["schema"])

    def field_title_should_be_set(self, schema) -> bool:
        return False


def input_schema(model: type[Args]) -> dict:
    schema = model.model_json_schema(schema_generator=_Schema)
    schema.pop("title", None)
    if not schema.get("required"):
        schema.pop("required", None)
    return {**schema, "additionalProperties": False}


def _bounds(model: type[Args], name: str) -> tuple:
    field = next((field for key, field in model.model_fields.items() if name in (key, field.alias)), None)
    meta = field.metadata if field else []
    return (next((item.ge for item in meta if isinstance(item, Ge)), None),
            next((item.le for item in meta if isinstance(item, Le)), None))


def _message(model: type[Args], error: dict) -> str:
    """The first validation error in words the agent passes on."""
    name = ".".join(str(part) for part in error["loc"]) or tr("Argumente")
    kind, context = error["type"], error.get("ctx") or {}
    if kind == "missing":
        return tr("{name} fehlt", name=name)
    if kind == "literal_error":
        choices = context["expected"].replace("'", "").replace(" or ", ", ")
        return tr("{name} muss einer von {choices} sein", name=name, choices=choices)
    if kind in ("greater_than_equal", "less_than_equal"):
        low, high = _bounds(model, name)
        return tr("{name} muss zwischen {low} und {high} liegen", name=name, low=low, high=high)
    if kind.startswith("bool"):
        return tr("{name} muss true oder false sein", name=name)
    if kind.startswith("int"):
        return tr("{name} muss eine ganze Zahl sein", name=name)
    if kind.startswith(("decimal", "float", "finite")):
        return tr("{name} muss eine Zahl sein", name=name)
    if kind.startswith("string"):
        return tr("{name} muss Text sein", name=name)
    return tr("{name} ist ungültig", name=name)


def _bind(model: type[Args], handler: Callable) -> dict:
    """inputSchema and handler of a tool; the handler takes the raw arguments and gets the checked model."""
    def run(arguments: dict) -> dict:
        try:
            args = model.model_validate(arguments)
        except ValidationError as error:
            raise ToolError(_message(model, error.errors()[0])) from None
        return handler(args)
    return {"inputSchema": input_schema(model), "handler": run}


def _zone() -> ZoneInfo:
    _, settings = config.get_settings()
    return local_zone(settings["timezone"])


def _at(raw: str | None) -> datetime:
    """The entry time: now unless the user named another time on purpose."""
    if raw is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    try:
        return parse_instant(str(raw))
    except ValueError:
        raise ToolError(tr("Zeit als ISO-8601 mit Zeitzone angeben, z. B. 2026-10-08T12:30:00+02:00")) from None


def _event_id(raw: str | None) -> str:
    try:
        return str(uuid.UUID(raw or str(uuid.uuid4())))
    except ValueError:
        raise ToolError(tr("event_id muss eine UUID sein")) from None


def _day(raw: str | None) -> str:
    if raw is None:
        return datetime.now(_zone()).date().isoformat()
    try:
        return date.fromisoformat(str(raw)).isoformat()
    except ValueError:
        raise ToolError(tr("day als JJJJ-MM-TT angeben")) from None


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
            "carbs_certainty", "fat_protein", "alcohol", "fast", "place", "dish", "description", "glucose_mgdl",
            "meal_id", "nightscout_synced")
    return [{key: (_local(item[key]) if key == "at" else (item[key] or {}).get("name") if key in ("place", "dish")
                   else item[key]) for key in keep if key in item}
            for item in current["timeline"]]


def _meals(day: str) -> list[dict]:
    """The day's main meals with their factor, the same rows as the read API."""
    rows = read_api.api_read("/api/v1/meals", urlencode({"from": day, "to": day}))["rows"]
    keep = ("meal_id", "meal", "at", "day_type", "window_start", "window_end", "end_reason", "excluded",
            "carbs_meal_g", "carbs_total_g", "rapid_units", "g0_mgdl", "g_end_mgdl", "delta_mgdl",
            "sport_effect_mgdl", "factor_ie_per_ke", "blocked_reason", "carbs_certainty", "fat_protein", "alcohol",
            "fast", "place", "dish")
    return [{key: row.get(key) for key in keep} for row in rows]


def get_status(args: GetStatusArgs) -> dict:
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


def _form(args: EntryArgs, at: datetime) -> dict:
    """Shared arguments of preview and save in the entry form's payload."""
    _, settings = config.get_settings()
    carbs = args.carbs_g or Decimal(0)
    return {
        "day": at.astimezone(local_zone(settings["timezone"])).date().isoformat(), "at": at.isoformat(),
        "mode": "meal", "carbs": str(carbs), "as": "meal" if args.own_main_meal else None, "split": args.split,
        "close_meal": args.close_meal is True,
        "finger_mgdl": args.finger_mgdl,
        # Certainty and fat decide whether the lower target is offered, so the preview needs them too.
        "carbs_certainty": args.carbs_certainty if carbs > 0 else None,
        "fat_protein": carbs > 0 and args.fat_protein is True,
        "fast": carbs > 0 and args.fast is True,
        # The calculated fat/protein surcharge (app/fpe.py), as the entry form sends it.
        **({"fat_g": args.fat_g, "protein_g": args.protein_g, "surcharge_mode": args.surcharge_mode} if carbs > 0 else {}),
    }


def _place_id(args: EntryArgs) -> int | None:
    """The place chip named by the agent; an unknown name lists the existing ones."""
    name = " ".join((args.place or "").split())
    if not name:
        return None
    options = place_store.all_places()
    found = next((place for place in options if place["name"].casefold() == name.casefold()), None)
    if found is None:
        raise ToolError(tr("Unbekannter Ort. Vorhandene Orte: ") + (", ".join(place["name"] for place in options)
                                                                 or tr("keine, Philipp legt sie in den Einstellungen an")))
    return found["id"]


def preview_entry(args: EntryArgs) -> dict:
    """What the entry form shows for these carbs: snack or main meal, basal, split, dose options."""
    at = _at(args.at)
    result = entry.preview(_form(args, at))
    assignment = result["assignment"]
    return {
        "at": _local(at), "day": result["day"],
        "assignment": assignment and {**assignment, "at": _local(assignment["at"])},
        "basal_suggestion_units": result["basal_units"],
        "day_type": result["day_type"], "split": result["split"],
        "dose": _dose_view(result["dose"]), "on_board": result["on_board"],
        "places": [place["name"] for place in place_store.all_places()],
    }


def save_entry(args: SaveEntryArgs) -> dict:
    """Save a meal, snack, correction or sport entry exactly as the entry form does."""
    kind = args.kind or "meal"
    at = _at(args.at)
    event_id = _event_id(args.event_id)
    payload = _form(args, at)
    insulin = args.insulin_units or Decimal(0)
    payload.update({
        "mode": kind, "insulin": str(insulin), "with_basal": args.with_basal is True,
        "dose_variant": args.dose_variant,
        "notes": (args.notes or "")[:500], "event_id": event_id, "place_id": _place_id(args),
        # A retry with the same event_id must not add a second basal dose either.
        "basal_event_id": str(uuid.uuid5(uuid.UUID(event_id), "basal")),
    })
    dish, description = " ".join((args.dish or "").split()), (args.description or "").strip()
    if kind == "meal" and (dish or description):
        payload.update({"dish_name": dish or None, "description": description})
    if kind == "exercise":
        sport_kind, minutes, change = args.sport_kind, args.duration_minutes, args.sport_change_mgdl
        if sport_kind is None or minutes is None:
            raise ToolError(tr("Sport braucht sport_kind und duration_minutes (ganze Minuten)"))
        payload.update({"sport_kind": sport_kind, "duration_minutes": minutes,
                        "sport_effect_mgdl": str(change) if change is not None else None})
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
        "basal_error": saved["basal_error"],
        "entry": next((item for item in _entries(current) if item["id"] == saved["id"]), None),
    }


def save_basal(args: SaveBasalArgs) -> dict:
    units = args.units
    at = _at(args.at)
    result = basal.save_basal({"at": at.isoformat(), "units": str(units), "event_id": _event_id(args.event_id),
                               "notes": (args.notes or "")[:500]})
    return {"saved": True, "at": _local(at), "units": str(units), **result}


def delete_entry(args: DeleteEntryArgs) -> dict:
    """Remove one entry of a day (Nightscout treatment or local basal) by the id get_day lists."""
    entry_id = args.id
    day = _day(args.day)
    current = day_view.day_payload(day, refresh=False)
    item = next((row for row in current["timeline"] if row["id"] == entry_id), None)
    if item is None:
        raise ToolError(tr("Eintrag an diesem Tag nicht gefunden; id und day aus get_day verwenden"))
    if item["kind"] == "basal":
        basal.delete_basal_entry({"id": entry_id})
    else:
        diary.delete_treatment({"id": entry_id, "day": day})
    return {"deleted": True, "day": day, "entry": _entries({"timeline": [item]})[0]}


def get_day(args: GetDayArgs) -> dict:
    day = _day(args.day)
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


def list_dishes(args: ListDishesArgs) -> dict:
    """The dish catalogue with each dish's outcome; `dish` narrows it to one dish with its recent meals."""
    current = dishes.view({"days": [str(args.days)]} if args.days else None)
    name = " ".join((args.dish or "").split()).casefold()
    if name:
        found = [dish for dish in current["dishes"] if dish["name"].casefold() == name]
        if not found:
            raise ToolError(tr("Unbekanntes Gericht. Vorhandene Gerichte: ")
                            + (", ".join(dish["name"] for dish in current["dishes"]) or "keine"))
        return {"days": current["days"], "dish": found[0]}
    return {"days": current["days"],
            "dishes": [{key: value for key, value in dish.items() if key != "history"} for dish in current["dishes"]]}


def read_data(args: ReadDataArgs) -> dict:
    """The read API (/api/v1) for longer periods and raw CGM values."""
    table = args.table or "meals"
    query = {key: str(value) for key, value in (("days", args.days), ("from", args.from_), ("to", args.to))
             if value is not None}
    try:
        return read_api.api_read(f"/api/v1/{table}", urlencode(query))
    except read_api.ApiNotFound:
        raise ToolError(tr("Unbekannte Tabelle")) from None


TOOLS = [
    {
        "name": "get_status",
        "title": tr("Wie läuft der Blutzucker?"),
        "description": tr("Aktueller CGM-Wert mit Trend, was noch wirkt (Insulin, KH, Aufschlag; erwarteter Wert "
                       "wenn alles gewirkt hat), offener zweiter Split-Teil, Korrekturvorschlag ohne Essen, "
                       "Basal heute und alle Einträge und Mahlzeitfaktoren von heute."),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        **_bind(GetStatusArgs, get_status),
    },
    {
        "name": "preview_entry",
        "title": tr("Empfehlung berechnen"),
        "description": tr("Vor jedem Eintrag aufrufen: zeigt, was das Eintragsformular für diese KH zeigt. "
                       "assignment = Snack einer Hauptmahlzeit (snack=true; meal=null = Snack ohne Mahlzeit, weil "
                       "die eigene Mahlzeit heute schon eingetragen ist) oder eigene Mahlzeit; Sport steckt "
                       "automatisch in on_board.sport_mgdl und der Dosis; basal_suggestion_units > 0 = Basal heute "
                       "noch offen (fragen, ob mit eintragen); split = Split-Bolus vorgeschlagen; dose.options = "
                       "normal / carbs (nur KH); slider = Spanne min_units..max_units mit recommended_units und corridor_mgdl "
                       "(10/25/50/75/90 %, Abweichung vom erwarteten BG expected_mgdl der Normal-Dosis; je IE mehr isf_mgdl_per_unit tiefer); "
                       "ohne KH nach einer Mahlzeit zusätzlich fast (wenig Bedarf, x0,6) / slow (viel, x1,4), suggested = Tendenz aus dem Anstieg; "
                       "bei Split units = jetzt und "
                       "later_units/later_at = zweiter Teil; ohne KH ggf. Option split = geplanter 2. Teil; mit KH bei offenem "
                       "split_plan nur die KH (der Anstieg bleibt beim 2. Teil). "
                       "Speichert nichts."),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        **_bind(EntryArgs, preview_entry),
    },
    {
        "name": "save_entry",
        "title": tr("Eintrag speichern"),
        "description": tr("Speichert Mahlzeit, Snack, Korrektur oder Sport in Nightscout, genau wie das "
                       "Eintragsformular. Nur aufrufen, wenn Philipp die Werte ausdrücklich bestätigt hat "
                       "(gegessene KH, tatsächlich gespritzte IE). Bei KH immer KH-Sicherheit und fett-/"
                       "eiweißreich mitgeben (erfragen, falls nicht genannt). Bei Split insulin_units = jetzt "
                       "gespritzter Teil, der Rest wird als geplanter 2. Teil vermerkt."),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        **_bind(SaveEntryArgs, save_entry),
    },
    {
        "name": "save_basal",
        "title": tr("Basal speichern"),
        "description": tr("Speichert eine Basal-Injektion (lokal und als Notiz in Nightscout). Nur auf Philipps "
                       "ausdrückliche Angabe."),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        **_bind(SaveBasalArgs, save_basal),
    },
    {
        "name": "delete_entry",
        "title": tr("Eintrag löschen"),
        "description": tr("Löscht einen Eintrag (auch Basal) eines Tages, z. B. einen falsch verstandenen. Nur auf "
                       "ausdrückliche Bitte und nach Nennung des Eintrags. id und day aus get_day/get_status."),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                        "openWorldHint": False},
        **_bind(DeleteEntryArgs, delete_entry),
    },
    {
        "name": "get_day",
        "title": tr("Tag anzeigen"),
        "description": tr("Ein Tagebuchtag: alle Einträge (mit id), Mahlzeiten mit Faktor (IE/KE) oder "
                       "Blockgrund, Tagesart und BZ-Kennzahlen."),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        **_bind(GetDayArgs, get_day),
    },
    {
        "name": "list_dishes",
        "title": tr("Gerichte und ihr Verlauf"),
        "description": tr("Gerichte-Katalog: je Gericht Anzahl Einträge, übliche KH und Ergebnis (ratio = "
                       "Mahlzeitfaktor ÷ üblicher Faktor der Mahlzeitenart in den 30 Tagen davor; 1,2 = brauchte "
                       "20 % mehr Insulin als üblich, effective_carbs = übliche KH × ratio). Mit dish: ein Gericht "
                       "samt seinen letzten Hauptmahlzeiten (KH, IE, Faktor, BZ vorher/nachher, Ort)."),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        **_bind(ListDishesArgs, list_dishes),
    },
    {
        "name": "read_data",
        "title": tr("Daten lesen"),
        "description": tr("Lese-API für längere Zeiträume: meals (Faktoren), window-events, entries (CGM), "
                       "treatments, basal, meal-records (Einträge mit Gericht/Beschreibung samt Ergebnis), data (alles), profile (Einstellungen und Nightscout-Profil)."),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        **_bind(ReadDataArgs, read_data),
    },
]
