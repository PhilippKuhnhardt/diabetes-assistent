"""Local settings, manual-window limits and the vacation switch."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import auth, calculation, episodes, forecast
from app.common import parse_instant, unit_text
from app.ingestion import nightscout, sync
from app.store import config, marks



def validate_settings(value: dict) -> dict:
    _, old = config.get_settings()
    result = dict(old)
    for key in ("timezone", "window_minutes", "peak_minutes", "duration_minutes",
                "breakfast_end_minute", "lunch_end_minute", "sport_lookback_minutes",
                "sport_effects_mgdl", "dose_target_mgdl", "dose_cautious_target_mgdl", "auto_percentile",
                "auto_half_life_days", "auto_lookback_days", "auto_method", "auto_surcharge_mgdl",
                "basal_default_units"):
        if key in value:
            result[key] = value[key]
    # Nightscout owns the correction-factor schedule; it is not part of local settings.
    result.pop("isf_schedule", None)
    result.pop("ic_schedule", None)
    result.pop("target_schedule", None)
    result.pop("nightscout_profile", None)
    if result.get("auto_method", "percentile") not in forecast.METHODS:
        raise ValueError("Unbekannte Methode der Automatik")
    ZoneInfo(result["timezone"])
    window = int(result["window_minutes"])
    peak = int(result["peak_minutes"])
    duration = int(result["duration_minutes"])
    if not (60 <= window <= 360 and 20 <= peak < duration <= 600):
        raise ValueError("Ungültige Zeitparameter")
    breakfast_end = int(result["breakfast_end_minute"])
    lunch_end = int(result["lunch_end_minute"])
    lookback = int(result["sport_lookback_minutes"])
    dose_target = calculation.dec(result["dose_target_mgdl"])
    if not dose_target.is_finite() or not 60 <= dose_target <= 140:
        raise ValueError("Zielwert der Spritzhilfe muss zwischen 60 und 140 mg/dL liegen")
    cautious_target = calculation.dec(result["dose_cautious_target_mgdl"])
    if not cautious_target.is_finite() or not dose_target <= cautious_target <= 250:
        raise ValueError("Vorsichtiger Zielwert muss zwischen dem Zielwert und 250 mg/dL liegen")
    auto_percentile = calculation.dec(result["auto_percentile"])
    if not auto_percentile.is_finite() or not 25 <= auto_percentile <= 50:
        raise ValueError("Perzentil der Automatik muss zwischen 25 und 50 % liegen")
    auto_lookback = int(result["auto_lookback_days"])
    if auto_lookback not in forecast.LOOKBACKS:
        raise ValueError("Zeitraum der Automatik muss 8, 15 oder 30 Tage sein")
    half_life = calculation.dec(result["auto_half_life_days"])
    if not half_life.is_finite() or not 1 <= half_life <= 28:
        raise ValueError("Halbwertszeit der Automatik muss zwischen 1 und 28 Tagen liegen")
    surcharge = calculation.dec(result.get("auto_surcharge_mgdl", forecast.SURCHARGE_MGDL))
    if not surcharge.is_finite() or not 5 <= surcharge <= 150:
        raise ValueError("Mahlzeitzuschlag muss zwischen 5 und 150 mg/dL liegen")
    basal_default = calculation.dec(result.get("basal_default_units", config.DEFAULT_BASAL_UNITS))
    if not basal_default.is_finite() or not 0 <= basal_default <= 100 or basal_default % Decimal("0.5"):
        raise ValueError("Basal-Standard muss zwischen 0 und 100 IE in 0,5er-Schritten liegen")
    effects = result["sport_effects_mgdl"]
    if not (0 < breakfast_end < lunch_end < 1440 and 0 <= lookback <= 1440):
        raise ValueError("Ungültige Mahlzeiten- oder Sportzeiten")
    if not isinstance(effects, dict) or set(effects) != set(episodes.SPORT_LABELS):
        raise ValueError("Ungültige Sportarten")
    clean_effects = {}
    for kind, amount in effects.items():
        effect = calculation.dec(amount)
        if not effect.is_finite() or not -500 <= effect <= 500:
            raise ValueError("Ungültiger Sporteffekt")
        clean_effects[kind] = str(effect)
    result.update({
        "window_minutes": window, "peak_minutes": peak, "duration_minutes": duration,
        "breakfast_end_minute": breakfast_end, "lunch_end_minute": lunch_end,
        "sport_lookback_minutes": lookback, "sport_effects_mgdl": clean_effects,
        "dose_target_mgdl": str(dose_target),
        "dose_cautious_target_mgdl": str(cautious_target),
        "auto_percentile": str(auto_percentile),
        "auto_half_life_days": str(half_life),
        "auto_lookback_days": auto_lookback,
        "auto_method": result.get("auto_method", "percentile"),
        "auto_surcharge_mgdl": str(surcharge),
        "basal_default_units": unit_text(basal_default),
    })
    return result


MANUAL_WINDOW_LEAD = timedelta(hours=2)
MANUAL_WINDOW_LAG = timedelta(hours=2)
MANUAL_WINDOW_MIN = timedelta(minutes=30)
MANUAL_WINDOW_MAX = timedelta(hours=10)
# The same limits in minutes, for the diary's charts and the window editor.
MANUAL_WINDOW_LIMITS = {
    "lead_minutes": MANUAL_WINDOW_LEAD // timedelta(minutes=1), "lag_minutes": MANUAL_WINDOW_LAG // timedelta(minutes=1),
    "min_minutes": MANUAL_WINDOW_MIN // timedelta(minutes=1), "max_minutes": MANUAL_WINDOW_MAX // timedelta(minutes=1),
}


def validate_manual_window(meal_at: datetime, start: datetime, end: datetime) -> None:
    """A manual window starts within two hours around the meal and ends after it."""
    if not meal_at - MANUAL_WINDOW_LEAD <= start <= meal_at + MANUAL_WINDOW_LAG:
        raise ValueError("Beginn muss höchstens zwei Stunden vor oder nach dem Mahlzeitbeginn liegen")
    if end < max(start, meal_at) + MANUAL_WINDOW_MIN:
        raise ValueError("Ende muss mindestens 30 Minuten nach Mahlzeit- und Fensterbeginn liegen")
    if end - start > MANUAL_WINDOW_MAX:
        raise ValueError("Zeitraum darf höchstens zehn Stunden lang sein")


def vacation_state() -> dict:
    current = next((period for period in marks.vacation_periods() if period["end_day"] is None), None)
    return {"active": bool(current), "since": current["start_day"] if current else None}


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
        "vacation": vacation_state(),
    }


def save_settings(payload: dict) -> dict:
    settings = validate_settings(payload)
    effective_at = parse_instant(str(payload["effective_at"])) if payload.get("effective_at") else None
    version = config.save_settings(settings, effective_at)
    return {"version": version, "settings": settings}


def set_vacation(payload: dict) -> dict:
    active = payload.get("active")
    if not isinstance(active, bool):
        raise ValueError("Ungültige Urlaubsauswahl")
    _, current_settings = config.get_settings()
    today = datetime.now(ZoneInfo(current_settings["timezone"])).date().isoformat()
    marks.set_vacation(active, today)
    return vacation_state()


def save_profile_schedule(path: str, payload: dict) -> dict:
    profile_id = payload.get("profile_id")
    is_ic = path.endswith("/ic")
    schedule = payload.get("ic_schedule" if is_ic else "isf_schedule")
    if not isinstance(profile_id, str) or not profile_id or not isinstance(schedule, list):
        raise ValueError("Ungültiger Profil-Zeitplan")
    if is_ic:
        converted = []
        for row in schedule:
            if "units_per_ke" in row:
                factor = calculation.dec(row["units_per_ke"])
                if not factor.is_finite() or factor <= 0:
                    raise ValueError("Insulinfaktor muss positiv sein")
                converted.append({"start_minute": row["start_minute"],
                                  "grams_per_unit": str(Decimal(10) / factor)})
            else:
                converted.append(row)
        schedule = converted
    profiles = nightscout.update_profile_schedule("carbratio" if is_ic else "sens", schedule, profile_id)
    config.mirror_nightscout_profiles(profiles)
    version, settings = config.get_settings()
    return {"version": version, "settings": settings,
            "message": ("I:C-Zeitplan" if is_ic else "Korrekturfaktor-Zeitplan") +
                       " in Nightscout gespeichert"}
