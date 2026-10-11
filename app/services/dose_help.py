"""Dose suggestion for an entry being typed today."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import dosing, forecast, on_board, split
from app.common import parse_instant
from app.services import day_view, factors
from app.store import config, marks, mirror
from app.analysis.stats import median, quartile


def dose_help(payload: dict) -> dict:
    _, current_settings = config.get_settings()
    today = datetime.now(ZoneInfo(current_settings["timezone"])).date().isoformat()
    synced = mirror.synced_at(today)
    cached_recently = bool(synced and datetime.now(timezone.utc) -
                           parse_instant(synced) < timedelta(minutes=1))
    current_day = day_view.day_payload(today, refresh=not cached_recently)
    entries = [{"at": parse_instant(row["at"]), "sgv": row["sgv"]}
               for row in current_day["entries"]]
    treatments = [{"at": parse_instant(row["at"]), **{k: v for k, v in row.items() if k != "at"}}
                  for row in current_day["treatments"]]
    now = datetime.now(timezone.utc)
    finger = [{"at": row["at"], "mgdl": row["finger_mgdl"]} for row in treatments if row.get("finger_mgdl")]
    if payload.get("finger_mgdl") is not None:
        # A value typed in the entry form counts before it is saved, at the entry's time.
        at = payload.get("finger_at") or now
        finger.append({"at": min(at, now), "mgdl": Decimal(str(payload["finger_mgdl"]))})
    local_now = now.astimezone(ZoneInfo(current_settings["timezone"]))
    overrides, periods = marks.analysis_day_types(), marks.vacation_periods()
    day_type, day_type_source = marks.day_type_for(today, overrides, periods)
    minute = local_now.hour * 60 + local_now.minute
    is_breakfast = minute < int(current_settings["breakfast_end_minute"])
    meal_kind = ("breakfast" if is_breakfast else
                 "lunch" if minute < int(current_settings["lunch_end_minute"]) else "dinner")
    meal_values = factors.meal_factor_values(meal_kind, day_type if is_breakfast else None,
                                     local_now.date(), overrides, periods)
    factor = ({"count": len(meal_values), "days": factors.BREAKFAST_FACTOR_DAYS,
               "median_units_per_ke": median(meal_values) if meal_values else None}
              if is_breakfast else None)
    use_factor = bool(factor and factor["count"] >= factors.BREAKFAST_FACTOR_MIN_MEALS
                      and factor["median_units_per_ke"] > 0)
    auto_percentile, half_life, lookback = factors.forecast_settings(current_settings)
    points = factors.meal_points(local_now.date(), lookback, overrides, periods)
    method = current_settings.get("auto_method", "percentile")
    surcharge_mgdl = factors.surcharge_mgdl(current_settings)
    prediction = forecast.predict_method(points, (meal_kind, day_type if is_breakfast else None),
                                         local_now.date(), auto_percentile, half_life, lookback, method,
                                         surcharge_mgdl)
    auto_value = prediction["units_per_ke"]
    carb_factor = factors.carb_factor(current_settings, local_now.date(), points, day_type,
                                      {(meal_kind, day_type if is_breakfast else None): auto_value})

    try:
        active = on_board.summary(on_board.sources(
            treatments, now, current_settings, carb_factor,
            surcharge_mgdl if method == forecast.SURCHARGE else None, forecast.surcharge_share), now)
    except ValueError:
        active = None
    # The surcharge belongs once to each main meal; a snack of a meal gets the factor only.
    surcharge = (method == forecast.SURCHARGE and auto_value is not None
                 and payload.get("snack") is not True)
    result = dosing.suggest(payload.get("carbs", 0), current_settings, entries, treatments, now,
                            activity_change=payload.get("activity_change_mgdl", 0),
                            units_per_ke=factor["median_units_per_ke"] if use_factor else None,
                            auto_units_per_ke=auto_value,
                            auto_surcharge_mgdl=surcharge_mgdl if surcharge else None,
                            surcharge_share=forecast.surcharge_share, on_board=active, finger=finger)
    plan = split.open_plan(treatments, now)
    result["split_plan"] = plan and {"meal_at": plan["meal_at"].isoformat(), "units": str(plan["units"]),
                                     "due_at": plan["due_at"].isoformat()}
    result["on_board"] = on_board_view(active, result) if active else None
    result.update({
        "auto_meal": meal_kind, "auto_factor_count": prediction["count"], "auto_method": method,
        "auto_factor_min_meals": factors.AUTO_FACTOR_MIN_MEALS, "auto_percentile": str(auto_percentile),
        "auto_half_life_days": str(half_life), "auto_factor_days": lookback,
        "auto_median_units_per_ke": str(median(meal_values)) if meal_values else None,
        "auto_q1_units_per_ke": (str(quartile(meal_values, Decimal("0.25")))
                                 if len(meal_values) >= factors.AUTO_FACTOR_MIN_MEALS else None),
        "day_type": day_type, "day_type_source": day_type_source, "breakfast": is_breakfast,
        "factor_source": "breakfast_day_type" if use_factor else "nightscout",
        "breakfast_factor_count": factor["count"] if factor else None,
        "breakfast_factor_days": factor["days"] if factor else None,
        "breakfast_factor_min_meals": factors.BREAKFAST_FACTOR_MIN_MEALS,
        "breakfast_factor_units_per_ke": (str(factor["median_units_per_ke"])
                                          if factor and factor["median_units_per_ke"] is not None else None),
    })
    return result


def on_board_view(active: dict, result: dict) -> dict:
    """The diary's "Wirkt noch" card: what still lowers and raises glucose, until when, and the
    expected value once all of it has acted (normal forecast, with its typical spread)."""
    instant = lambda value: value.isoformat() if value else None
    fresh = result.get("glucose_fresh")
    return {
        "glucose_mgdl": result.get("glucose_mgdl"), "glucose_fresh": bool(fresh),
        "glucose_source": result.get("glucose_source"),
        "insulin_units": str(active["insulin_units"]),
        "lowering_mgdl": str(active["lowering_mgdl"]), "lowering_until": instant(active["lowering_until"]),
        "raising_mgdl": str(active["raising_mgdl"]), "raising_until": instant(active["raising_until"]),
        "carbs_mgdl": str(active["carbs_mgdl"]), "surcharge_mgdl": str(active["surcharge_mgdl"]),
        "carbs_remaining_g": str(sum((row["remaining_grams"] for row in active["carbs"]), Decimal(0))),
        "expected_mgdl": (str(Decimal(result["glucose_mgdl"]) - active["lowering_mgdl"] + active["raising_mgdl"])
                          if fresh else None),
        "expected_at": instant(active["settled_at"]),
        "spread_mgdl": str(on_board.FORECAST_SPREAD_MGDL),
        "fresh_meal_at": instant(active["fresh_meal_at"]),
        "split_plan": result.get("split_plan"),
        "doses": [{"at": dose["at"].isoformat(), "units": str(dose["units"]),
                   "remaining_units": str(dose["remaining_units"])} for dose in active["doses"]],
        "carbs": [{"at": row["at"].isoformat(), "grams": str(row["grams"]),
                   "remaining_grams": str(row["remaining_grams"])} for row in active["carbs"]],
    }
