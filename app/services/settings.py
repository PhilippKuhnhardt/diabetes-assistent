"""Local settings, manual-window limits and the vacation switch."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator

from app import auth, calculation, core, episodes, forecast, fpe
from app.common import local_zone, parse_instant, unit_text, zone_name_at
from app.ingestion import nightscout, sync
from app.store import config, marks, timezones
from app.i18n import tr



SPLIT_MESSAGE = tr("Split-Bolus: ab 0–300 g (0 = aus), 30–90 % sofort, Rest nach 30–300 min")
# Every field's own message, used for any problem with it (missing, no number, out of range).
MESSAGES = {
    "timezone": tr("Unbekannte Zeitzone"),
    "window_minutes": tr("Ungültige Zeitparameter"),
    "breakfast_end_minute": tr("Ungültige Mahlzeiten- oder Sportzeiten"),
    "lunch_end_minute": tr("Ungültige Mahlzeiten- oder Sportzeiten"),
    "sport_lookback_minutes": tr("Ungültige Mahlzeiten- oder Sportzeiten"),
    "dose_target_mgdl": tr("Zielwert der Spritzhilfe muss zwischen 60 und 140 mg/dL liegen"),
    "dose_cautious_target_mgdl": tr("Vorsichtiger Zielwert muss zwischen dem Zielwert und 250 mg/dL liegen"),
    "auto_percentile": tr("Perzentil der Automatik muss zwischen 25 und 50 % liegen"),
    "auto_lookback_days": tr("Zeitraum der Automatik muss 8, 15 oder 30 Tage sein"),
    "auto_half_life_days": tr("Halbwertszeit der Automatik muss zwischen 1 und 28 Tagen liegen"),
    "auto_method": tr("Unbekannte Methode der Automatik"),
    "auto_surcharge_mgdl": tr("Mahlzeitzuschlag muss zwischen 5 und 150 mg/dL liegen"),
    "auto_fat_surcharge_mgdl": tr("Fett-Zuschlag muss zwischen 0 und 200 mg/dL liegen"),
    "fpe_surcharge_mgdl": tr("Zuschlag je FPE muss zwischen 5 und 60 mg/dL liegen"),
    "auto_vacation": tr("Unbekannte Urlaubs-Einstellung der Automatik"),
    "day_types": tr("Arbeit / Frei muss an oder aus sein"),
    "glucose_unit": tr("Unbekannte Blutzucker-Einheit"),
    "carb_unit": tr("Unbekannte Einheit für den Insulinfaktor"),
    "low_limit_mgdl": tr("Grenze für tief muss zwischen 54 und 90 mg/dL liegen"),
    "high_limit_mgdl": tr("Grenze für hoch muss zwischen 180 und 400 mg/dL liegen"),
    "basal_default_units": tr("Basal-Standard muss zwischen 0 und 100 IE in 0,5er-Schritten liegen"),
    "split_min_grams": SPLIT_MESSAGE, "split_first_percent": SPLIT_MESSAGE, "split_delay_minutes": SPLIT_MESSAGE,
    "split_min_units": tr("Split-Bolus: Mindestdosis 0–30 IE in 0,5er-Schritten (0 = immer)"),
    "sport_effects_mgdl": tr("Ungültiger Sporteffekt"),
    "sport_durations_minutes": tr("Sport-Wirkdauer muss zwischen 30 und 1440 Minuten liegen"),
    "sport_custom": tr("Eigene Sportart: Name mit 1 bis 40 Zeichen, jeder Name nur einmal"),
    "sport_hidden": tr("Ungültige Sportarten"),
}


# A built-in kind or one the user added (setting `sport_custom`); which keys exist is checked between fields.
SportKind = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class SettingsInput(BaseModel):
    """The local calculation settings: types and ranges per field, rules between fields below."""
    model_config = ConfigDict(extra="ignore")

    timezone: str
    window_minutes: Annotated[int, Field(ge=60, le=360)]
    breakfast_end_minute: Annotated[int, Field(gt=0)]
    lunch_end_minute: Annotated[int, Field(lt=1440)]
    sport_lookback_minutes: Annotated[int, Field(ge=0, le=1440)]
    dose_target_mgdl: Annotated[Decimal, Field(ge=60, le=140)]
    dose_cautious_target_mgdl: Annotated[Decimal, Field(le=250)]
    auto_percentile: Annotated[Decimal, Field(ge=25, le=50)]
    auto_lookback_days: int
    auto_half_life_days: Annotated[Decimal, Field(ge=1, le=28)]
    auto_method: Literal[forecast.METHODS] = "percentile"
    auto_surcharge_mgdl: Annotated[Decimal, Field(ge=5, le=150)] = forecast.SURCHARGE_MGDL
    auto_fat_surcharge_mgdl: Annotated[Decimal, Field(ge=0, le=200)] = forecast.FAT_SURCHARGE_MGDL
    fpe_surcharge_mgdl: Annotated[Decimal, Field(ge=5, le=60)] = fpe.PER_FPE_MGDL
    auto_vacation: Literal[config.AUTO_VACATION_MODES] = config.DEFAULT_AUTO_VACATION
    day_types: StrictBool = False
    glucose_unit: Literal[config.GLUCOSE_UNITS] = "mgdl"
    carb_unit: Literal[config.CARB_UNITS] = "ke"
    low_limit_mgdl: Annotated[Decimal, Field(ge=54, le=90)] = Decimal(config.DEFAULT_LIMITS["low_limit_mgdl"])
    high_limit_mgdl: Annotated[Decimal, Field(ge=180, le=400)] = Decimal(config.DEFAULT_LIMITS["high_limit_mgdl"])
    basal_default_units: Annotated[Decimal, Field(ge=0, le=100, multiple_of=Decimal("0.5"))] = \
        Decimal(str(config.DEFAULT_BASAL_UNITS))
    split_min_grams: Annotated[int, Field(ge=0, le=300)] = config.DEFAULT_SPLIT["split_min_grams"]
    split_min_units: Annotated[Decimal, Field(ge=0, le=30, multiple_of=Decimal("0.5"))] = \
        Decimal(config.DEFAULT_SPLIT["split_min_units"])
    split_first_percent: Annotated[int, Field(ge=30, le=90)] = config.DEFAULT_SPLIT["split_first_percent"]
    split_delay_minutes: Annotated[int, Field(ge=30, le=300)] = config.DEFAULT_SPLIT["split_delay_minutes"]
    sport_effects_mgdl: dict[SportKind, Annotated[Decimal, Field(ge=-500, le=500)]]
    # Whole minutes: 300 and "300" pass, a fraction is refused instead of cut.
    sport_durations_minutes: dict[SportKind, Annotated[int, Field(ge=30, le=1440)]] | None = None
    # The user's own sport kinds (key -> name) and the kinds the entry form no longer offers. A kind is never
    # deleted, only hidden, so sessions recorded with it keep their name and effect.
    sport_custom: dict[SportKind, Annotated[str, Field(min_length=1, max_length=40)]] = {}
    sport_hidden: list[SportKind] = []

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(MESSAGES["timezone"]) from None
        return value

    @field_validator("auto_lookback_days")
    @classmethod
    def _lookback(cls, value: int) -> int:
        if value not in forecast.LOOKBACKS:
            raise ValueError(MESSAGES["auto_lookback_days"])
        return value

    @field_validator("sport_custom", mode="before")
    @classmethod
    def _custom_names(cls, value):
        if not isinstance(value, dict):
            raise ValueError(MESSAGES["sport_custom"])
        names = {key: " ".join(str(name).split()) for key, name in value.items()}
        folded = [name.casefold() for name in [*episodes.SPORT_NAMES.values(), *names.values()]]
        if set(names) & set(episodes.SPORT_LABELS) or len(set(folded)) != len(folded):
            raise ValueError(MESSAGES["sport_custom"])
        return names

    @model_validator(mode="after")
    def _every_sport(self):
        kinds = set(episodes.SPORT_LABELS) | set(self.sport_custom)
        if (set(self.sport_effects_mgdl) != kinds or set(self.sport_hidden) - kinds
                or (self.sport_durations_minutes is not None and set(self.sport_durations_minutes) != kinds)):
            raise ValueError(tr("Ungültige Sportarten"))
        return self

    @model_validator(mode="after")
    def _between_fields(self):
        if not self.breakfast_end_minute < self.lunch_end_minute:
            raise ValueError(MESSAGES["breakfast_end_minute"])
        if not self.dose_target_mgdl <= self.dose_cautious_target_mgdl:
            raise ValueError(MESSAGES["dose_cautious_target_mgdl"])
        return self


def _settings_error(error: ValidationError) -> str:
    first = error.errors()[0]
    if first["type"] == "value_error":
        return str(first["ctx"]["error"])
    return MESSAGES.get(str(first["loc"][0]) if first["loc"] else "", tr("Ungültige Einstellungen"))


def validate_settings(value: dict) -> dict:
    _, old = config.get_settings()
    result = dict(old)
    for key in SettingsInput.model_fields:
        if key in value:
            result[key] = value[key]
    # Nightscout owns the correction-factor schedule; it is not part of local settings.
    # peak_minutes / duration_minutes: former settings that no calculation read (the insulin curve is
    # app/insulin.py); older stored versions still carry them.
    for key in ("isf_schedule", "ic_schedule", "target_schedule", "nightscout_profile", "peak_minutes",
                "duration_minutes"):
        result.pop(key, None)
    try:
        checked = SettingsInput.model_validate(result)
    except ValidationError as error:
        raise ValueError(_settings_error(error)) from None
    result.update({
        **checked.model_dump(include={"timezone", "window_minutes",
                                      "breakfast_end_minute", "lunch_end_minute", "sport_lookback_minutes",
                                      "auto_lookback_days", "auto_method", "auto_vacation", "day_types", "glucose_unit", "carb_unit",
                                      "sport_custom",
                                      *config.DEFAULT_SPLIT}),
        **{key: str(getattr(checked, key)) for key in (
            "dose_target_mgdl", "dose_cautious_target_mgdl", "auto_percentile",
            "auto_half_life_days", "auto_surcharge_mgdl", "auto_fat_surcharge_mgdl", "fpe_surcharge_mgdl",
            "low_limit_mgdl", "high_limit_mgdl")},
        "sport_effects_mgdl": {kind: str(amount) for kind, amount in checked.sport_effects_mgdl.items()},
        "sport_durations_minutes": checked.sport_durations_minutes or {
            kind: config.DEFAULT_SPORT_DURATIONS.get(kind, 300) for kind in checked.sport_effects_mgdl},
        "sport_hidden": sorted(set(checked.sport_hidden)),
        "basal_default_units": unit_text(checked.basal_default_units),
        "split_min_units": unit_text(checked.split_min_units),
    })
    return result


def validate_manual_window(meal_at: datetime, start: datetime, end: datetime) -> None:
    """A manual window starts within two hours around the meal, ends at least 30 minutes after meal and window
    start and lasts at most ten hours (the rule: window.rs problem)."""
    problem = core.call("window.problem", meal_at=meal_at, start=start, end=end)
    if problem:
        raise ValueError(problem)


VACATION_BACKDATE_DAYS = 14


def vacation_state() -> dict:
    periods = marks.vacation_periods()
    current = next((period for period in periods if period["end_day"] is None), None)
    return {"active": bool(current), "since": current["start_day"] if current else None,
            "periods": periods[-5:], "backdate_days": VACATION_BACKDATE_DAYS}


TIMEZONE_BACKDATE_DAYS = 14


def _offset_minutes(name: str, at: datetime) -> int:
    return int(at.astimezone(ZoneInfo(name)).utcoffset().total_seconds() // 60)


def travel_state(now: datetime | None = None) -> dict:
    """Home zone, the zone in effect now and the latest switches."""
    now = now or datetime.now(timezone.utc)
    _, current_settings = config.get_settings()
    home = current_settings["timezone"]
    current = zone_name_at(home, now)
    switches = timezones.switches()
    return {"home": home, "current": current, "current_offset_minutes": _offset_minutes(current, now),
            "home_offset_minutes": _offset_minutes(home, now),
            "switches": [{"at": at.isoformat(), "zone": name or home, "home": not name}
                         for at, name in switches[-5:]],
            "backdate_days": TIMEZONE_BACKDATE_DAYS}


def set_timezone(payload: dict, now: datetime | None = None) -> dict:
    """Switch the local clock from an instant on (default now), back home with zone '' or the home
    zone's name, or {"undo": true} drops the latest switch."""
    now = now or datetime.now(timezone.utc)
    if payload.get("undo") is True:
        if not timezones.delete_last():
            raise ValueError(tr("Es gibt keinen Zeitzonen-Wechsel zum Zurücknehmen"))
        return travel_state(now)
    _, current_settings = config.get_settings()
    home = current_settings["timezone"]
    name = payload.get("zone")
    if not isinstance(name, str):
        raise ValueError(tr("Ungültige Zeitzone"))
    name = name.strip()
    if name in ("", home):
        name = ""
    else:
        try:
            ZoneInfo(name)
        except (ValueError, KeyError, OSError):
            raise ValueError(tr("Unbekannte Zeitzone")) from None
    at = now
    if payload.get("at"):
        try:
            at = parse_instant(str(payload["at"]))
        except ValueError:
            raise ValueError(tr("Ungültiger Zeitpunkt")) from None
        if not now - timedelta(days=TIMEZONE_BACKDATE_DAYS) <= at <= now + timedelta(minutes=5):
            raise ValueError(tr("Zeitpunkt muss zwischen jetzt und vor {days} Tagen liegen", days=TIMEZONE_BACKDATE_DAYS))
    switches = timezones.switches()
    if switches and at <= switches[-1][0]:
        raise ValueError(tr("Zeitpunkt muss nach dem letzten Zeitzonen-Wechsel liegen"))
    if (switches[-1][1] if switches else "") == name:
        raise ValueError(tr("Diese Zeitzone gilt bereits"))
    timezones.add_switch(at, name)
    return travel_state(now)


def settings_view() -> dict:
    profile_sync_error = None
    if nightscout.is_configured():
        try:
            sync.sync_profile_cache()
        except nightscout.NightscoutError as error:
            profile_sync_error = str(error)
    version, settings = config.get_settings()
    profile_sync = config.profile_sync_status()
    return {
        "version": version, "settings": settings, "connected": nightscout.is_configured(),
        "login_required": bool(auth.password()),
        "profile_sync_at": profile_sync["synced_at"], "profile_sync_error": profile_sync_error,
        "vacation": vacation_state(), "travel": travel_state(),
    }


def save_settings(payload: dict) -> dict:
    settings = validate_settings(payload)
    effective_at = parse_instant(str(payload["effective_at"])) if payload.get("effective_at") else None
    version = config.save_settings(settings, effective_at)
    return {"version": version, "settings": settings}


def set_vacation(payload: dict) -> dict:
    active = payload.get("active")
    if not isinstance(active, bool):
        raise ValueError(tr("Ungültige Urlaubsauswahl"))
    _, current_settings = config.get_settings()
    today = datetime.now(local_zone(current_settings["timezone"])).date()
    # A switch made late (on in the evening of the second day, off a day after coming home) can name
    # the first vacation day or the first day back, up to two weeks ago.
    day = today
    if payload.get("day"):
        try:
            day = date.fromisoformat(str(payload["day"]))
        except ValueError:
            raise ValueError(tr("Ungültiges Datum")) from None
        if not today - timedelta(days=VACATION_BACKDATE_DAYS) <= day <= today:
            raise ValueError(tr("Datum muss zwischen heute und vor {days} Tagen liegen", days=VACATION_BACKDATE_DAYS))
    marks.set_vacation(active, day.isoformat())
    return vacation_state()


def save_profile_schedule(path: str, payload: dict) -> dict:
    profile_id = payload.get("profile_id")
    is_ic = path.endswith("/ic")
    schedule = payload.get("ic_schedule" if is_ic else "isf_schedule")
    if not isinstance(profile_id, str) or not profile_id or not isinstance(schedule, list):
        raise ValueError(tr("Ungültiger Profil-Zeitplan"))
    if is_ic:
        converted = []
        for row in schedule:
            if "units_per_ke" in row:
                factor = calculation.dec(row["units_per_ke"])
                if not factor.is_finite() or factor <= 0:
                    raise ValueError(tr("Insulinfaktor muss positiv sein"))
                converted.append({"start_minute": row["start_minute"],
                                  "grams_per_unit": str(Decimal(10) / factor)})
            else:
                converted.append(row)
        schedule = converted
    profiles = nightscout.update_profile_schedule("carbratio" if is_ic else "sens", schedule, profile_id)
    config.mirror_nightscout_profiles(profiles)
    version, settings = config.get_settings()
    return {"version": version, "settings": settings,
            "message": tr("I:C-Zeitplan in Nightscout gespeichert") if is_ic
            else tr("Korrekturfaktor-Zeitplan in Nightscout gespeichert")}
