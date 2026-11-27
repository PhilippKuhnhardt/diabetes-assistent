"""Analysis page: factor groups, Automatik derivation, night and sport overview."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from app import calculation, episodes, forecast, sport_analysis
from app.common import MEALS, json_safe, local_zone
from app.ingestion import nightscout, sync
from app.services import day_view, factors
from app.store import config, marks, mirror, results
from app.analysis.stats import median, quartile, strong_outlier_flags
from app.i18n import tr


def summary(params: dict) -> dict:
    days = int(params.get("days", ["14"])[0])
    if days not in (1, 3, 7, 14, 30):
        raise ValueError(tr("Ungültiger Zeitraum"))
    profile_sync_error = None
    sync_errors = []
    _, settings = config.get_settings()
    today = datetime.now(local_zone(settings["timezone"])).date()
    if nightscout.is_configured():
        try:
            sync.sync_profile_cache()
        except nightscout.NightscoutError as error:
            profile_sync_error = str(error)
        # The page loads its period by itself (user request 2026-11-26, replacing "Zeitraum aus Nightscout
        # laden"): the background job keeps yesterday and today current, and every day the analysis reads
        # that was never mirrored is fetched once here.
        missing = mirror.never_synced([(today - timedelta(days=offset)).isoformat()
                                       for offset in range(history_days(days) + 1)])
        if missing:
            _, sync_errors = sync.mirror_days(missing, settings["timezone"])
            memo = getattr(day_view._pass, "memo", None)
            if memo is not None:
                memo.clear()  # inside a background round the memo holds these days as they were before
    # Stored per period while today, the exclusions and every day it reads are unchanged.
    fingerprint = hashlib.sha256(json.dumps([
        day_view.CODE_VERSION, today.isoformat(), sorted(marks.analysis_exclusions()),
        [day_view.day_fingerprint((today - timedelta(days=offset)).isoformat())
         for offset in range(history_days(days) + 1)],
    ]).encode()).hexdigest()
    result = results.summary_result(str(days), fingerprint)
    if result is None:
        result = json_safe(compute_summary(days, today))
        results.save_summary_result(str(days), fingerprint, result)
    result["profile_sync_error"] = profile_sync_error
    result["sync_error_days"] = [item["day"] for item in sync_errors]
    return result


def history_days(days: int) -> int:
    """How many days before today the analysis reads (period, 90-day matrix with lookback, stress tile)."""
    return max(max(days - 1, forecast.BACKTEST_DAYS - 1) + forecast.MAX_LOOKBACK + 1,
               forecast.RATIO_DAYS + forecast.RATIO_REFERENCE_DAYS)


def compute_summary(days: int, today: date) -> dict:
    """The analysis page for the last `days` days, from stored diary days only."""
    profile_sync_error = None
    _, settings = config.get_settings()
    excluded_ids = marks.analysis_exclusions()
    day_types = marks.analysis_day_types()
    vacations = marks.vacation_periods()
    groups = {name: {"total": 0, "count": 0, "valid_count": 0, "excluded_count": 0,
                     "blocked_count": 0, "values": [], "nightscout_values": [],
                     "calculations": [], "multi_carb_count": 0, "shortened_count": 0}
              for name in MEALS}
    night_records = []
    quiet_records = []
    sport_meals = []
    sport_sessions = []
    loaded_days = {}
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        payload = loaded_days[day] = day_view.day_payload(day, refresh=False)
        night_records.append(night_record(day, payload))
        quiet_records.extend({"day": day, **item} for item in payload.get("quiet_periods") or [])
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
            quality = meal.get("quality") or {}
            item["certainty"] = quality.get("carbs_certainty")
            item["fast"] = bool(quality.get("fast"))
            item["dish"] = (meal.get("dish") or {}).get("name")
            item["expected_delta_mgdl"] = item["deviation_mgdl"] = item["expected_units_per_ke"] = None
            item["_calculation"] = result
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
        if group is groups["breakfast"] and day_types is not None:
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
    for kind in (("work", "free") if day_types is not None else ()):
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
    surcharge_mgdl = factors.surcharge_mgdl(settings)
    points = factors.meal_points(today, history_days(days), day_types, vacations, loaded_days,
                                 mode=factors.vacation_mode(settings))
    # The Automatik part (backtest matrix, tuning rows, trade-offs) is by far the costliest and reads
    # only these meal factors and the settings, the same for every period. It is reused while they
    # are unchanged: across the five periods of a background round, and across rounds while new CGM
    # readings change no meal factor.
    key = json.dumps([today.isoformat(), settings, points], default=str, sort_keys=True)
    cached = _automatik_cache.get(key)
    if cached is None:
        # Each meal's miss against its own Automatik suggestion reads the same inputs, so it is kept
        # with them: Erwartet/Delta of every period and the weekly trend look it up.
        point_misses = {point["id"]: suggestion_miss(points, point, settings) for point in points}
        cached = (*_automatik(today, points, settings), point_misses)
        _automatik_cache.clear()  # only the latest is ever asked for again
        _automatik_cache[key] = cached
    predictions, suggestion_derivation, day_type_ratio, point_misses = cached
    misses = expect_from_automatik(groups, points, settings, point_misses)
    trend = miss_trend(points, settings, today, point_misses)
    for kind in breakfast["by_day_type"]:
        breakfast["by_day_type"][kind]["suggestion"] = factors.suggestion_factor(
            predictions[("breakfast", kind)], configured, surcharge_mgdl)
    for name in ("lunch", "dinner") if day_types is not None else ("breakfast", "lunch", "dinner"):
        groups[name]["suggestion"] = factors.suggestion_factor(predictions[(name, None)], configured, surcharge_mgdl)
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
        "days": days, "groups": groups, "misses": misses, "trend": trend, "profile_sync_error": profile_sync_error,
        "suggestion_derivation": json_safe(suggestion_derivation),
        "day_types": day_types is not None,
        "day_type_ratio": json_safe(day_type_ratio),
        "sport_analysis": sport_view(sport_meals, sport_sessions, settings),
        "night": night_overview(night_records, days),
        "quiet": quiet_overview(quiet_records),
    }


def expect_from_automatik(groups: dict, points: list[dict], settings: dict, point_misses: dict | None = None) -> dict:
    """What each meal was expected to do (user decision 2026-11-27): the glucose change had its factor
    been the Automatik suggestion that meal would have got from the days before it, with the doses given.
    Delta = measured − expected = (actual − suggested factor) × Σ[g × KF] / 10, the same factor error the
    backtest counts. Adds the typical miss per meal kind and per carb certainty and "Schnell" mark.
    `point_misses` holds suggestion_miss() by meal id when the caller already computed it."""
    by_id = {point["id"]: point for point in points}
    misses = {"certainty": {}, "fast": {}}
    for group in groups.values():
        for item in group["calculations"]:
            result = item.pop("_calculation")
            point = by_id.get(item["id"])
            if point is None or result.get("delta_mgdl") is None:
                continue  # blocked or excluded: no comparison
            suggested, deviation = (point_misses[point["id"]] if point_misses is not None
                                    else suggestion_miss(points, point, settings))
            if suggested is None:
                continue  # too few meals before it
            item["expected_units_per_ke"] = str(suggested)
            item["expected_delta_mgdl"] = str(Decimal(result["delta_mgdl"]) - deviation)
            item["deviation_mgdl"] = str(deviation)
            misses["certainty"].setdefault(item["certainty"] or "none", []).append(deviation)
            misses["fast"].setdefault("fast" if item["fast"] else "other", []).append(deviation)
        values = [Decimal(item["deviation_mgdl"]) for item in group["calculations"] if item["deviation_mgdl"] is not None]
        group.update(miss_stats(values))
        group["expected_count"] = len(values)
    return {kind: {level: {"count": len(values), **miss_stats(values)} for level, values in levels.items()}
            for kind, levels in misses.items()}


def suggestion_miss(points: list[dict], point: dict, settings: dict) -> tuple[Decimal | None, Decimal | None]:
    """The Automatik suggestion a meal would have got from the days before it, and the factor error in
    mg/dL: (actual − suggested) × Σ[g × KF] / 10 (> 0: the meal ended higher, it needed more)."""
    if not point.get("carbs_isf"):
        return None, None
    percentile, half_life, lookback = factors.forecast_settings(settings)
    if settings.get("auto_method", "percentile") == forecast.SURCHARGE:
        mgdl, fat_mgdl = factors.surcharge_mgdl(settings), factors.fat_surcharge_mgdl(settings)
        values = forecast.base_history(points, point["key"], point["day"], lookback, mgdl, fat_mgdl)
        suggested = forecast.predict_surcharge(values, percentile, half_life, point, mgdl, fat_mgdl)
    else:
        suggested = forecast.predict(points, point["key"], point["day"], percentile, half_life, lookback)["units_per_ke"]
    if suggested is None:
        return None, None
    return suggested, (point["value"] - suggested) * point["carbs_isf"] / 10


TREND_WEEKS = 8
DRIFT_WEEKS = 3
DRIFT_MGDL = Decimal(20)


def miss_trend(points: list[dict], settings: dict, today: date, point_misses: dict | None = None) -> dict:
    """The miss week by week (user question 2026-11-27: is there a trend the Automatik does not follow
    fast enough?): per meal kind the median signed miss of each of the last TREND_WEEKS weeks before
    today, independent of the analysis period like the backtest matrix. `drift` names a direction when
    the last DRIFT_WEEKS weeks with meals all missed the same way by at least DRIFT_MGDL."""
    weeks = [{"start": (today - timedelta(days=7 * (index + 1))).isoformat(),
              "end": (today - timedelta(days=7 * index + 1)).isoformat()} for index in reversed(range(TREND_WEEKS))]
    buckets: dict[str, list[list[Decimal]]] = {meal: [[] for _ in weeks] for meal in MEALS}
    for point in points:
        age = (today - point["day"]).days
        if not 1 <= age <= 7 * TREND_WEEKS or point.get("own_place") or point["key"][0] not in buckets:
            continue
        deviation = (point_misses[point["id"]] if point_misses is not None
                     else suggestion_miss(points, point, settings))[1]
        if deviation is not None:
            buckets[point["key"][0]][TREND_WEEKS - 1 - (age - 1) // 7].append(deviation)
    kinds = {}
    for meal, columns in buckets.items():
        cells = [{"count": len(values), **miss_stats(values)} for values in columns]
        recent = [Decimal(cell["median_deviation_mgdl"]) for cell in cells if cell["count"]][-DRIFT_WEEKS:]
        drift = None
        if len(recent) == DRIFT_WEEKS and all(value >= DRIFT_MGDL for value in recent):
            drift = "up"
        elif len(recent) == DRIFT_WEEKS and all(value <= -DRIFT_MGDL for value in recent):
            drift = "down"
        kinds[meal] = {"weeks": cells, "drift": drift}
    return {"weeks": weeks, "kinds": kinds}


def miss_stats(deviations: list[Decimal]) -> dict:
    """The typical miss (median of |Delta|) and its direction (median Delta: > 0 ended higher than expected)."""
    if not deviations:
        return {"median_abs_deviation_mgdl": None, "median_deviation_mgdl": None}
    return {"median_abs_deviation_mgdl": str(median(sorted(abs(value) for value in deviations))),
            "median_deviation_mgdl": str(median(sorted(deviations)))}


def night_record(day: str, payload: dict) -> dict:
    """One night of the night overview: the day's night analysis plus the basal injected that day."""
    night = payload.get("night") or {}
    keys = ("start", "end", "dinner_at", "breakfast_at", "last_injection_at", "morning_dose_at", "delta_mgdl",
            "start_glucose", "end_glucose", "minimum_glucose", "minimum_at", "drop_mgdl", "rise_mgdl", "carbs_g", "reason")
    basal = sum((Decimal(str(dose["units"])) for dose in payload.get("basal") or []), Decimal(0))
    return {"day": day, **{key: night.get(key) for key in keys},
            "basal_units": str(basal) if payload.get("basal") else None}


def sport_view(meals: list[dict], sessions: list[dict], settings: dict) -> dict:
    """The sport analysis, with an "Übernehmen" for a kind whose measured effect suggests another standard."""
    standards = {kind: Decimal(str(value)) for kind, value in settings["sport_effects_mgdl"].items()}
    result = sport_analysis.analyze(meals, sessions, settings["sport_lookback_minutes"], settings["window_minutes"],
                                    episodes.sport_names(config.get_settings()[1]), standards,
                                    set(marks.sport_session_effects()))
    for group in result["kinds"]:
        suggested = group["suggested_drop_mgdl"]
        group["standard_drop_mgdl"] = str(standards[group["kind"]]) if group["kind"] in standards else None
        group["apply"] = ({"sport_effects_mgdl": {**settings["sport_effects_mgdl"], group["kind"]: suggested}}
                          if suggested is not None else None)
    return result


def quiet_overview(records: list[dict]) -> dict:
    """Stretches without bolus and food (night.quiet_periods), newest first, with the median change per
    hour weighted by nothing: each stretch counts once."""
    rates = sorted(Decimal(str(item["rate_mgdl_per_hour"])) for item in records)
    hours = sum((Decimal(str(item["hours"])) for item in records), Decimal(0))
    return {"count": len(records), "hours": str(hours),
            "median_rate_mgdl_per_hour": str(median(rates)) if rates else None,
            "periods": sorted(records, key=lambda item: str(item["start"]), reverse=True)}


def night_overview(records: list[dict], days: int) -> dict:
    """How the nights went (user request 2026-11-26: does the basal hold?): medians of the fall from the
    night start to its lowest value and of the rise from there to the morning, over complete nights
    without carbs eaten in them (a hypo snack hides how far glucose would have fallen)."""
    complete = [item for item in records if item["minimum_glucose"] is not None and item["start_glucose"] is not None]
    clean = [item for item in complete if not Decimal(str(item["carbs_g"] or 0))]

    def middle(key):
        values = sorted(Decimal(str(item[key])) for item in clean if item[key] is not None)
        return str(median(values)) if values else None

    return {
        "count": len(clean), "with_carbs_count": len(complete) - len(clean),
        "unavailable_count": days - len(complete),
        "median_start_glucose": middle("start_glucose"), "median_end_glucose": middle("end_glucose"),
        "median_minimum_glucose": middle("minimum_glucose"), "median_delta_mgdl": middle("delta_mgdl"),
        "median_drop_mgdl": middle("drop_mgdl"), "median_rise_mgdl": middle("rise_mgdl"),
        "nights": records,
    }


# The last Automatik part computed, by its inputs (see compute_summary). Callers only read it.
_automatik_cache: dict[str, tuple[dict, dict, dict, dict]] = {}


def _automatik(today: date, points: list[dict], settings: dict) -> tuple[dict, dict, dict]:
    """The period-independent part of the analysis page: the suggestions, the backtest matrix with
    the Automatik view, and the day-type ratio, from the given meal factors."""
    configured = factors.forecast_settings(settings)
    percentile, half_life, lookback = configured
    method = settings.get("auto_method", "percentile")
    surcharge_mgdl = factors.surcharge_mgdl(settings)
    active_surcharge = factors.active_surcharge_mgdl(settings)
    fat_mgdl = factors.fat_surcharge_mgdl(settings)
    predictions = {key: {**forecast.predict_method(points, key, today, percentile, half_life, lookback, method,
                                                   surcharge_mgdl, fat_mgdl),
                         "surcharge": method == forecast.SURCHARGE}
                   for key in factors.meal_keys(settings)}
    # Backtest matrix: every lookback × half-life (plus the configured ones), each at 25 %,
    # the configured percentile and 50 %, all evaluated on the same meals of the last 90 days.
    # A last column tests the configured percentile with the fixed surcharge per main meal.
    percentiles = [*sorted({Decimal(25), percentile, Decimal(50)}), forecast.SURCHARGE]
    rows = [(days_back, strength) for days_back in sorted({*forecast.LOOKBACKS, lookback})
            for strength in sorted({*forecast.HALF_LIVES, half_life})]
    since = today - timedelta(days=forecast.BACKTEST_DAYS - 1)
    target = calculation.dec(settings["dose_target_mgdl"])
    test = forecast.backtest(points, since,
                             [(days_back, strength, value) for days_back, strength in rows
                              for value in percentiles],
                             target, percentile, surcharge_mgdl, fat_mgdl)
    cells = iter(test["stats"])
    suggestion_derivation = {
        "percentile": str(percentile), "half_life_days": str(half_life), "lookback_days": lookback,
        "method": method, "surcharge_mgdl": str(surcharge_mgdl), "fat_surcharge_mgdl": str(fat_mgdl),
        "surcharge_full_grams": str(forecast.SURCHARGE_FULL_GRAMS),
        "min_meals": forecast.MIN_MEALS, "meal_count": len(test["rows"]),
        "backtest_days": forecast.BACKTEST_DAYS,
        "variants": [{"lookback_days": days_back, "half_life_days": strength,
                      "cells": [{"percentile": value, "stats": next(cells)} for value in percentiles]}
                     for days_back, strength in rows],
        "glucose_low_mgdl": str(forecast.GLUCOSE_LOW), "glucose_high_mgdl": str(forecast.GLUCOSE_HIGH),
        "dose_target_mgdl": str(settings["dose_target_mgdl"]),
    }
    mark_matrix(suggestion_derivation)
    # The Automatik view: one row per setting around the configured ones, and a worked example per meal.
    suggestion_derivation["tuning"] = [
        {**row, "options": [{**option, "apply": tuning_apply(row["key"], option["value"])} for option in row["options"]]}
        for row in forecast.tuning(points, since, lookback, half_life, percentile, active_surcharge, target,
                                   fat_mgdl)]
    suggestion_derivation["active_surcharge_mgdl"] = str(active_surcharge)
    tradeoffs = forecast.tradeoffs(points, since, lookback, half_life, percentile, active_surcharge, target, fat_mgdl)
    for option in [tradeoffs["current"], *tradeoffs["line"]]:
        option["apply"] = {}
        for key, value in (("lookback", option["lookback_days"]), ("half_life", option["half_life_days"]),
                           ("percentile", option["percentile"]), ("surcharge", option["surcharge_mgdl"])):
            if key in option["changes"]:
                option["apply"].update(tuning_apply(key, value))
    suggestion_derivation["tradeoffs"] = tradeoffs
    suggestion_derivation["examples"] = [
        forecast.explain(points, key, today, lookback, half_life, percentile, active_surcharge, fat_mgdl)
        for key in factors.meal_keys(settings)]
    suggestion_derivation["weights"] = [
        {"age_days": age, "weight": forecast.weight(age, half_life)}
        for age in sorted({1, *(int(half_life * k) for k in (1, 2, 4)), lookback}) if 1 <= age <= lookback]
    # Information only: does the free-day factor follow the work-day factor by a stable ratio?
    day_type_ratio = forecast.day_type_ratios(points, today) if config.day_types_enabled(settings) else None
    return predictions, suggestion_derivation, day_type_ratio


def tuning_apply(key: str, value) -> dict:
    """The settings change that makes a tuning option the configured one."""
    if key == "percentile":
        return {"auto_percentile": str(value)}
    if key == "half_life":
        return {"auto_half_life_days": str(value)}
    if key == "lookback":
        return {"auto_lookback_days": int(value)}
    return ({"auto_method": forecast.SURCHARGE, "auto_surcharge_mgdl": str(value)} if value
            else {"auto_method": "percentile"})


def mark_matrix(derivation: dict) -> None:
    """Add to every backtest cell its shares below / in / above range, whether it is the configured
    variant, and the best cells: most endpoints in range, then fewest below, once per lookback
    period and once overall (the first of equal cells wins)."""
    def better(cell, top):
        stats, best = cell["stats"], top["stats"] if top else None
        return best is None or stats["in_range"] > best["in_range"] or (
            stats["in_range"] == best["in_range"] and stats["below"] < best["below"])

    overall, per_lookback = None, {}
    for variant in derivation["variants"]:
        for cell in variant["cells"]:
            stats = cell["stats"]
            total = stats["glucose_count"]
            cell["shares"] = {key: (Decimal(stats[key]) * 100 / total if total else Decimal(0))
                              for key in ("below", "in_range", "above")}
            surcharge = cell["percentile"] == forecast.SURCHARGE
            cell["active"] = ((surcharge if derivation["method"] == forecast.SURCHARGE
                               else not surcharge and Decimal(cell["percentile"]) == Decimal(derivation["percentile"]))
                              and variant["lookback_days"] == derivation["lookback_days"]
                              and Decimal(variant["half_life_days"]) == Decimal(derivation["half_life_days"]))
            cell["best"] = None
            if better(cell, overall):
                overall = cell
            if better(cell, per_lookback.get(variant["lookback_days"])):
                per_lookback[variant["lookback_days"]] = cell
    for cell in per_lookback.values():
        cell["best"] = "lookback"
    if overall is not None:
        overall["best"] = "overall"
