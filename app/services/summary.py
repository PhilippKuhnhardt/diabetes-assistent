"""Analysis page: factor groups, Automatik derivation, night and sport overview."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import calculation, episodes, forecast, nightscout, sport_analysis
from app.store import config, marks
from app.analysis.stats import median, quartile, strong_outlier_flags
from app.common import MEALS, json_safe
from app.services import day_view, factors, sync


def summary(params: dict) -> dict:
    days = int(params.get("days", ["14"])[0])
    if days not in (1, 3, 7, 14, 30):
        raise ValueError("Ungültiger Zeitraum")
    profile_sync_error = None
    if nightscout.is_configured():
        try:
            sync.sync_profile_cache()
        except nightscout.NightscoutError as error:
            profile_sync_error = str(error)
    _, settings = config.get_settings()
    today = datetime.now(ZoneInfo(settings["timezone"])).date()
    excluded_ids = marks.analysis_exclusions()
    day_types = marks.analysis_day_types()
    vacations = marks.vacation_periods()
    groups = {name: {"total": 0, "count": 0, "valid_count": 0, "excluded_count": 0,
                     "blocked_count": 0, "values": [], "nightscout_values": [],
                     "calculations": [], "multi_carb_count": 0, "shortened_count": 0}
              for name in MEALS}
    night_records = []
    night_start_values = []
    night_end_values = []
    night_minimum_values = []
    night_delta_values = []
    sport_meals = []
    sport_sessions = []
    loaded_days = {}
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        payload = loaded_days[day] = day_view.day_payload(day, refresh=False)
        night = payload.get("night") or {}
        night_item = {
            "day": day, "start": night.get("start"), "end": night.get("end"),
            "dinner_at": night.get("dinner_at"), "breakfast_at": night.get("breakfast_at"),
            "last_injection_at": night.get("last_injection_at"),
            "delta_mgdl": night.get("delta_mgdl"),
            "start_glucose": night.get("start_glucose"),
            "end_glucose": night.get("end_glucose"),
            "minimum_glucose": night.get("minimum_glucose"),
            "minimum_at": night.get("minimum_at"), "reason": night.get("reason"),
        }
        night_records.append(night_item)
        if all(night_item[key] is not None for key in
               ("start_glucose", "end_glucose", "minimum_glucose")):
            night_start_values.append(Decimal(night_item["start_glucose"]))
            night_end_values.append(Decimal(night_item["end_glucose"]))
            night_minimum_values.append(Decimal(night_item["minimum_glucose"]))
            night_delta_values.append(Decimal(night_item["delta_mgdl"]))
        day_type, _ = marks.day_type_for(day, day_types, vacations)
        sport_sessions.extend(
            {"id": item["id"], "at": item["at"], "duration_minutes": item["duration_minutes"],
             "kind": item["sport_kind"], "notes": item["notes"]}
            for item in payload.get("timeline", []) if item["kind"] == "exercise")
        sport_meals.extend(
            {"id": meal["id"], "day": day, "meal": meal["meal"], "at": meal["at"],
             "excluded": meal["id"] in excluded_ids, "calculation": meal.get("calculation") or {},
             "sport": meal.get("sport") or {}}
            for meal in payload["meals"])
        for meal in payload["meals"]:
            group = groups[meal["meal"]]
            group["total"] += 1
            result = meal.get("calculation") or {}
            if result.get("end_reason") == "before_next_meal":
                group["shortened_count"] += 1
            if result.get("extra_carb_count", 0):
                group["multi_carb_count"] += 1
            factor_text = result.get("factor_units_per_ke")
            excluded = meal["id"] in excluded_ids
            group["calculations"].append({
                "id": meal["id"], "day": day, "at": meal["at"],
                "day_type": day_type, "units_per_ke": factor_text,
                "carbs_total": result.get("carbs_total"),
                "rapid_units_in_window": result.get("rapid_units_in_window"),
                "delta_mgdl": result.get("delta_mgdl"),
                "end": result.get("end"),
                "manual_window": bool(result.get("manual_window")),
                "reason": result.get("reason"),
                "excluded": excluded,
                "sport": meal.get("sport", {}),
            })
            item = group["calculations"][-1]
            item["expected_delta_mgdl"] = None
            item["deviation_mgdl"] = None
            if (factor_text is not None and settings["ic_schedule"]
                    and result.get("carb_events") is not None
                    and result.get("insulin_effect_mgdl") is not None
                    and result.get("delta_mgdl") is not None):
                carb_effect = sum((
                    Decimal(event["carbs"]) * Decimal(event["isf"]) /
                    calculation.schedule_value_at(
                        datetime.fromisoformat(event["at"]), settings["ic_schedule"],
                        settings["timezone"], "grams_per_unit")
                    for event in result["carb_events"]), Decimal(0))
                expected = carb_effect - Decimal(result["insulin_effect_mgdl"]) - Decimal(result.get("sport_effect_mgdl", "0"))
                item["expected_delta_mgdl"] = str(expected)
                item["deviation_mgdl"] = str(Decimal(result["delta_mgdl"]) - expected)
            if factor_text is None:
                group["blocked_count"] += 1
            else:
                group["valid_count"] += 1
                if excluded:
                    group["excluded_count"] += 1
                else:
                    group["count"] += 1
                    group["values"].append(Decimal(factor_text))
                    group["nightscout_values"].append(Decimal(result["ratio_g_per_unit"]))
    for group in groups.values():
        comparable = [item for item in group["calculations"] if item["units_per_ke"] is not None]
        for item in group["calculations"]:
            item["strong_outlier"] = False
        if group is groups["breakfast"]:
            comparison_sets = [[item for item in comparable if item["day_type"] == kind]
                               for kind in ("work", "free")]
        else:
            comparison_sets = [comparable]
        for comparison in comparison_sets:
            flags = strong_outlier_flags([Decimal(item["units_per_ke"]) for item in comparison])
            for item, flag in zip(comparison, flags):
                item["strong_outlier"] = flag
        group["strong_outlier_count"] = sum(item["strong_outlier"] for item in comparable)
        values = sorted(group.pop("values"))
        ns_values = sorted(group.pop("nightscout_values"))
        group["median_units_per_ke"] = str(median(values)) if values else None
        group["mean_units_per_ke"] = str(sum(values) / len(values)) if values else None
        group["median_g_per_unit"] = str(median(ns_values)) if ns_values else None
        group["min_units_per_ke"] = str(values[0]) if values else None
        group["max_units_per_ke"] = str(values[-1]) if values else None
        # With fewer than five meals, quartiles imply more precision than the data support.
        group["q1_units_per_ke"] = str(quartile(values, Decimal("0.25"))) if len(values) >= 5 else None
        group["q3_units_per_ke"] = str(quartile(values, Decimal("0.75"))) if len(values) >= 5 else None
        chronological = sorted(
            (item for item in group["calculations"]
             if item["units_per_ke"] is not None and not item["excluded"]),
            key=lambda item: item["at"], reverse=True)
        if len(chronological) >= 6:
            recent = median(sorted(Decimal(item["units_per_ke"]) for item in chronological[:3]))
            previous = median(sorted(Decimal(item["units_per_ke"]) for item in chronological[3:6]))
            group["recent_median_units_per_ke"] = str(recent)
            group["previous_median_units_per_ke"] = str(previous)
            group["trend_units_per_ke"] = str(recent - previous)
        else:
            group["recent_median_units_per_ke"] = None
            group["previous_median_units_per_ke"] = None
            group["trend_units_per_ke"] = None
    breakfast = groups["breakfast"]
    breakfast["by_day_type"] = {}
    for kind in ("work", "free"):
        items = [item for item in breakfast["calculations"] if item["day_type"] == kind]
        values = sorted(Decimal(item["units_per_ke"]) for item in items
                        if item["units_per_ke"] is not None and not item["excluded"])
        breakfast["by_day_type"][kind] = {
            "total": len(items), "count": len(values),
            "valid_count": sum(item["units_per_ke"] is not None for item in items),
            "excluded_count": sum(item["units_per_ke"] is not None and item["excluded"] for item in items),
            "blocked_count": sum(item["units_per_ke"] is None for item in items),
            "mean_units_per_ke": str(sum(values) / len(values)) if values else None,
            "median_units_per_ke": str(median(values)) if values else None,
            "min_units_per_ke": str(values[0]) if values else None,
            "max_units_per_ke": str(values[-1]) if values else None,
            "q1_units_per_ke": str(quartile(values, Decimal("0.25"))) if len(values) >= 5 else None,
            "q3_units_per_ke": str(quartile(values, Decimal("0.75"))) if len(values) >= 5 else None,
        }
    configured = factors.forecast_settings(settings)
    percentile, half_life, lookback = configured
    points = factors.meal_points(today, max(max(days - 1, forecast.BACKTEST_DAYS - 1) + forecast.MAX_LOOKBACK + 1,
                                     forecast.RATIO_DAYS + forecast.RATIO_REFERENCE_DAYS),
                         day_types, vacations, loaded_days)
    method = settings.get("auto_method", "percentile")
    predictions = {key: {**forecast.predict_method(points, key, today, percentile, half_life, lookback, method),
                         "surcharge": method == forecast.SURCHARGE}
                   for key in (("breakfast", "work"), ("breakfast", "free"),
                               ("lunch", None), ("dinner", None))}
    for kind in ("work", "free"):
        breakfast["by_day_type"][kind]["suggestion"] = factors.suggestion_factor(
            predictions[("breakfast", kind)], configured)
    for name in ("lunch", "dinner"):
        groups[name]["suggestion"] = factors.suggestion_factor(predictions[(name, None)], configured)
    # Backtest matrix: every lookback × half-life (plus the configured ones), each at 25 %,
    # the configured percentile and 50 %, all evaluated on the same meals of the last 90 days.
    # A last column tests the configured percentile with the fixed surcharge per main meal.
    percentiles = [*sorted({Decimal(25), percentile, Decimal(50)}), forecast.SURCHARGE]
    rows = [(days_back, strength) for days_back in sorted({*forecast.LOOKBACKS, lookback})
            for strength in sorted({*forecast.HALF_LIVES, half_life})]
    test = forecast.backtest(points, today - timedelta(days=forecast.BACKTEST_DAYS - 1),
                             [(days_back, strength, value) for days_back, strength in rows
                              for value in percentiles],
                             calculation.dec(settings["dose_target_mgdl"]), percentile)
    cells = iter(test["stats"])
    suggestion_derivation = {
        "percentile": str(percentile), "half_life_days": str(half_life), "lookback_days": lookback,
        "method": method, "surcharge_mgdl": str(forecast.SURCHARGE_MGDL),
        "surcharge_full_grams": str(forecast.SURCHARGE_FULL_GRAMS),
        "min_meals": forecast.MIN_MEALS, "meal_count": len(test["rows"]),
        "backtest_days": forecast.BACKTEST_DAYS,
        "variants": [{"lookback_days": days_back, "half_life_days": strength,
                      "cells": [{"percentile": value, "stats": next(cells)} for value in percentiles]}
                     for days_back, strength in rows],
        "glucose_low_mgdl": str(forecast.GLUCOSE_LOW), "glucose_high_mgdl": str(forecast.GLUCOSE_HIGH),
        "dose_target_mgdl": str(settings["dose_target_mgdl"]),
    }
    # Information only: does the free-day factor follow the work-day factor by a stable ratio?
    day_type_ratio = forecast.day_type_ratios(points, today)
    for name, group in groups.items():
        current = set()
        if settings["ic_schedule"]:
            for item in group["calculations"]:
                grams = calculation.schedule_value_at(
                    datetime.fromisoformat(item["at"]), settings["ic_schedule"],
                    settings["timezone"], "grams_per_unit")
                current.add(Decimal(10) / grams)
        group["nightscout_units_per_ke"] = [str(value) for value in sorted(current)]
    return {
        "days": days, "groups": groups, "profile_sync_error": profile_sync_error,
        "suggestion_derivation": json_safe(suggestion_derivation),
        "day_type_ratio": json_safe(day_type_ratio),
        "sport_analysis": sport_analysis.analyze(
            sport_meals, sport_sessions, settings["sport_lookback_minutes"],
            settings["window_minutes"], episodes.SPORT_LABELS),
        "night": {
            "count": len(night_start_values),
            "unavailable_count": days - len(night_start_values),
            "median_start_glucose": str(median(sorted(night_start_values))) if night_start_values else None,
            "median_end_glucose": str(median(sorted(night_end_values))) if night_end_values else None,
            "median_minimum_glucose": str(median(sorted(night_minimum_values))) if night_minimum_values else None,
            "median_delta_mgdl": str(median(sorted(night_delta_values))) if night_delta_values else None,
            "nights": night_records,
        },
    }
