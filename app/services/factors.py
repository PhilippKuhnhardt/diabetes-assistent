"""Calculated meal factors over past days, as the dose help and analysis use them."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from zoneinfo import ZoneInfo

from app import calculation, forecast
from app.common import MEALS, parse_instant
from app.services import day_view
from app.store import config, marks
from app.store import places as place_store


BREAKFAST_FACTOR_DAYS = 28
BREAKFAST_FACTOR_MIN_MEALS = 3
AUTO_FACTOR_MIN_MEALS = forecast.MIN_MEALS


def counted_days(today: date, days_back: int, periods: list[dict], mode: str) -> list[tuple[date, date]]:
    """(day, day as the Automatik counts it) from today back over days_back days, newest first.

    Vacation meals (user request 2026-11-06, setting auto_vacation): outside a vacation, modes
    "separate" and "vacation_only" skip vacation days and move the days before them up by the
    vacation's length, so after coming home the lookback and the half-life reach the meals before
    the vacation as if it had not happened, and new meals take over as usual. During a vacation
    "separate" counts every day as before; "vacation_only" counts only vacation days. "all" counts
    every day everywhere."""
    calendar = [today - timedelta(days=offset) for offset in range(days_back + 1)]
    if mode not in ("separate", "vacation_only") or not periods:
        return [(day, day) for day in calendar]
    if marks.in_vacation(today.isoformat(), periods):
        if mode == "vacation_only":
            calendar = [day for day in calendar if marks.in_vacation(day.isoformat(), periods)]
        return [(day, day) for day in calendar]
    result = []
    offset = 0
    # Bounded so a pathological set of periods cannot loop for long.
    while len(result) <= days_back and offset <= days_back + 732:
        day = today - timedelta(days=offset)
        offset += 1
        if not marks.in_vacation(day.isoformat(), periods):
            result.append((day, today - timedelta(days=len(result))))
    return result


def vacation_mode(settings: dict) -> str:
    return settings.get("auto_vacation", config.DEFAULT_AUTO_VACATION)


def meal_factor_values(meal_kind: str, day_type: str | None, today: date,
                       overrides: dict[str, str], periods: list[dict],
                       mode: str = config.DEFAULT_AUTO_VACATION) -> list[Decimal]:
    """Sorted IE/KE of one meal kind over the past 28 days (excluded meals left out; vacation days
    as counted_days counts them). A day_type restricts the days to work or free days, as used for breakfast."""
    excluded = marks.analysis_exclusions()
    values = []
    for real_day, _ in counted_days(today, BREAKFAST_FACTOR_DAYS, periods, mode)[1:]:
        day = real_day.isoformat()
        if day_type is not None and marks.day_type_for(day, overrides, periods)[0] != day_type:
            continue
        for meal in day_view.day_payload(day, refresh=False).get("meals", []):
            factor = (meal.get("calculation") or {}).get("factor_units_per_ke")
            if meal.get("meal") == meal_kind and meal["id"] not in excluded and factor is not None:
                values.append(Decimal(factor))
    return sorted(values)


def suggestion_factor(prediction: dict, settings: tuple[Decimal, Decimal, int],
                      surcharge_mgdl: Decimal = forecast.SURCHARGE_MGDL) -> dict:
    """The auto factor the dose help uses, with the number of meals it is based on."""
    value = prediction["units_per_ke"]
    return {
        "auto_units_per_ke": str(value) if value is not None else None,
        "count": prediction["count"], "days": settings[2], "min_meals": AUTO_FACTOR_MIN_MEALS,
        "percentile": str(settings[0]), "half_life_days": str(settings[1]),
        "surcharge": prediction.get("surcharge", False),
        "surcharge_mgdl": str(surcharge_mgdl), "surcharge_full_grams": str(forecast.SURCHARGE_FULL_GRAMS),
    }


def meal_points(today: date, days_back: int, overrides: dict[str, str], periods: list[dict],
                loaded: dict[str, dict] | None = None, mode: str = config.DEFAULT_AUTO_VACATION) -> list[dict]:
    """Valid, non-excluded meal factors from today back to days_back days, for the suggestion.
    Breakfast is keyed by the day type of its day. `day` is the day as the Automatik counts it
    (counted_days: vacation days skipped after a vacation), `real_day` the calendar day."""
    excluded = marks.analysis_exclusions()
    own_places = {place["id"] for place in place_store.all_places() if place.get("adjust")}
    points = []
    for real_day, day in counted_days(today, days_back, periods, mode):
        text = real_day.isoformat()
        payload = loaded[text] if loaded is not None and text in loaded else day_view.day_payload(text, refresh=False)
        if loaded is not None:
            loaded[text] = payload
        day_type = marks.day_type_for(text, overrides, periods)[0]
        for meal in payload.get("meals", []):
            result = meal.get("calculation") or {}
            factor = result.get("factor_units_per_ke")
            if meal.get("meal") not in MEALS or meal["id"] in excluded or factor is None:
                continue
            at = (parse_instant(meal["at"]) if meal.get("at")
                  else datetime.combine(real_day, time(hour=12), tzinfo=timezone.utc))
            points.append({
                "id": meal["id"], "day": day, "real_day": real_day, "at": at,
                "key": (meal["meal"], day_type if meal["meal"] == "breakfast" else None),
                "day_type": day_type,
                "value": Decimal(factor),
                "end_glucose": Decimal(result["g3"]) if result.get("g3") is not None else None,
                "carbs_isf": (Decimal(result["carbs_times_isf_total"])
                              if result.get("carbs_times_isf_total") is not None else None),
                "carbs_g": Decimal(result["carbs_total"]) if result.get("carbs_total") is not None else None,
                "fat": bool((meal.get("quality") or {}).get("fat_protein")),
                "place_id": (meal.get("place") or {}).get("id"),
                # A meal at a place with its own place factor stays out of the Automatik's history.
                "own_place": (meal.get("place") or {}).get("id") in own_places,
            })
    return points


def surcharge_mgdl(settings: dict) -> Decimal:
    """Configured meal surcharge (mg/dL per main meal); used only with the surcharge method."""
    return Decimal(str(settings.get("auto_surcharge_mgdl", forecast.SURCHARGE_MGDL)))


def fat_surcharge_mgdl(settings: dict) -> Decimal:
    """Extra surcharge of a fat/protein main meal (mg/dL); counts only with the surcharge method."""
    return Decimal(str(settings.get("auto_fat_surcharge_mgdl", forecast.FAT_SURCHARGE_MGDL)))


def meal_surcharge_mgdl(settings: dict, fat: bool) -> Decimal | None:
    """Surcharge of one main meal as the dose help and "Wirkt noch" use it; None without the surcharge method."""
    if settings.get("auto_method") != forecast.SURCHARGE:
        return None
    return surcharge_mgdl(settings) + (fat_surcharge_mgdl(settings) if fat else Decimal(0))


def place_scales(settings: dict, today: date, overrides: dict[str, str], periods: list[dict]) -> dict[int, dict]:
    """The place factor of every place switched on for it (forecast.place_scale), by place id, with its name."""
    chosen = [place for place in place_store.all_places() if place.get("adjust")]
    if not chosen:
        return {}
    percentile, half_life, lookback = forecast_settings(settings)
    points = meal_points(today, lookback + forecast.PLACE_LOOKBACK_DAYS, overrides, periods,
                         mode=vacation_mode(settings))
    method = settings.get("auto_method", "percentile")
    return {place["id"]: {"name": place["name"], **forecast.place_scale(
                points, place["id"], today, percentile, half_life, lookback, method,
                surcharge_mgdl(settings), fat_surcharge_mgdl(settings))}
            for place in chosen}


def entry_scale(scales: dict[int, dict]):
    """scale(item) for on_board.sources: the place factor of the place recorded for a diary entry, else None."""
    if not any(item["scale"] for item in scales.values()):
        return None
    places = place_store.entry_places()
    return lambda item: (scales.get(places.get(item.get("event_id") or "")) or {}).get("scale")


def active_surcharge_mgdl(settings: dict) -> Decimal:
    """The surcharge the suggestion uses: the configured one with the surcharge method, else 0."""
    return surcharge_mgdl(settings) if settings.get("auto_method") == forecast.SURCHARGE else Decimal(0)


def forecast_settings(settings: dict) -> tuple[Decimal, Decimal, int]:
    """Configured percentile, half-life and lookback of the Automatik factor."""
    return (Decimal(str(settings["auto_percentile"])), Decimal(str(settings["auto_half_life_days"])),
            int(settings["auto_lookback_days"]))


def carb_factor(settings: dict, today: date, points: list[dict], day_type: str | None,
                predictions: dict | None = None):
    """IE/KE that values a carb entry made at `at`: the Automatik factor of its meal kind on `today`
    (breakfast keyed by the day type), else Nightscout's I:C at that time, else None."""
    predictions = {} if predictions is None else predictions
    percentile, half_life, lookback = forecast_settings(settings)
    method = settings.get("auto_method", "percentile")

    def value(at: datetime) -> Decimal | None:
        local = at.astimezone(ZoneInfo(settings["timezone"]))
        minute = local.hour * 60 + local.minute
        kind = ("breakfast" if minute < int(settings["breakfast_end_minute"]) else
                "lunch" if minute < int(settings["lunch_end_minute"]) else "dinner")
        key = (kind, day_type if kind == "breakfast" else None)
        if key not in predictions:
            predictions[key] = forecast.predict_method(points, key, today, percentile, half_life, lookback,
                                                       method, surcharge_mgdl(settings),
                                                       fat_surcharge_mgdl(settings))["units_per_ke"]
        if predictions[key] is not None:
            return predictions[key]
        try:
            return Decimal(10) / calculation.schedule_value_at(at, settings.get("ic_schedule", []),
                                                               settings["timezone"], "grams_per_unit")
        except ValueError:
            return None
    return value
