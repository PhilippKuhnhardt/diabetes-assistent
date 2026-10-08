"""Suggested meal factor (Automatik) from recent calculated meal factors.

The suggestion is a percentile of the meal factors of the same meal kind from the last
`lookback` days (8, 15 or 30; 8 so that on Sunday the previous Saturday still counts), each meal weighted by 0.5 ** (age in days / half-life).
A percentile depends on the order of values, not their size, so one odd day can move it
by at most its weight share, however extreme it is.

The backtest recomputes, for every past meal, what each combination of lookback, half-life and
percentile would have suggested from the meals before it, and estimates where the endpoint
glucose would have landed with that factor and a dose corrected to the target (below, inside or
above 80–160 mg/dL): target + (F − P) × Σ[g × KF] / 10.
The surcharge method splits each meal into a factor per KE and a fixed surcharge per main meal:
the meal's own fat and protein raise glucose by a configured amount (default SURCHARGE_MGDL) regardless of its carbs, growing
linearly up to the full amount at SURCHARGE_FULL_GRAMS so small meals get less. Past factors are
reduced by the surcharge's share, F − 10 × Z × share / Σ[g × KF], before the weighted percentile;
the dose then adds the surcharge once per main meal (Z × share / KF IE). Snacks get no surcharge.
A surcharge of 0 is the plain percentile.

The tuning rows vary one setting at a time around the configured ones (percentile, surcharge,
half-life, lookback), each replayed on the largest set of meals all its options can predict.
These are model choices for experimentation, not dosing advice.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache

LOOKBACKS = (8, 15, 30)
MAX_LOOKBACK = max(LOOKBACKS)
MIN_MEALS = 4
HALF = Decimal("0.5")
# Half-lives compared in the backtest: four stronger weightings, the default and one weaker.
HALF_LIVES = (Decimal(1), Decimal(2), Decimal(3), Decimal(5), Decimal(7), Decimal(14))
# Range for the endpoint glucose three hours after a meal; deliberately tighter than the
# Nightscout target range (80–180), which also covers the post-meal rise.
GLUCOSE_LOW = Decimal(80)
GLUCOSE_HIGH = Decimal(160)
# Work-stress surcharge: last 30 days, each free-day meal against the two nearest work-day meals
# before and the two after it, searched at most 7 days away.
RATIO_DAYS = 30
# The backtest matrix always replays the last 90 days, whatever period the analysis page shows.
BACKTEST_DAYS = 90
RATIO_NEIGHBOURS = 2
RATIO_REFERENCE_DAYS = 7


def weighted_quantile(pairs: list[tuple[Decimal, Decimal]], fraction: Decimal) -> Decimal:
    """Quantile of (value, weight) pairs; equal weights give linear interpolation at (n − 1) × p.
    Each value sits at the midpoint of its cumulative weight, rescaled so the smallest value
    is at 0 and the largest at 1."""
    items = sorted(pairs)
    if len(items) == 1:
        return items[0][0]
    total = sum(weight for _, weight in items)
    span = total - items[0][1] / 2 - items[-1][1] / 2
    positions = []
    running = Decimal(0)
    for _, weight in items:
        positions.append((running + weight / 2 - items[0][1] / 2) / span)
        running += weight
    for index in range(len(items) - 1):
        low, high = positions[index], positions[index + 1]
        if fraction <= high:
            share = (fraction - low) / (high - low) if high > low else Decimal(0)
            return items[index][0] + (items[index + 1][0] - items[index][0]) * max(Decimal(0), share)
    return items[-1][0]


def history(points: list[dict], key: tuple, day: date, lookback: int) -> list[tuple[Decimal, int]]:
    """(factor, age in days) of the same meal key from the lookback days before day; today is left out."""
    result = []
    for point in points:
        age = (day - point["day"]).days
        if point["key"] == key and 1 <= age <= lookback:
            result.append((point["value"], age))
    return result


@lru_cache(maxsize=1024)
def weight(age: int, half_life: Decimal) -> Decimal:
    return HALF ** (Decimal(age) / half_life)


def predict(points: list[dict], key: tuple, day: date, percentile: Decimal,
            half_life: Decimal, lookback: int) -> dict:
    """Weighted percentile of the meal key's factors before day; None below MIN_MEALS meals."""
    return predict_from(history(points, key, day, lookback), percentile, half_life)


def predict_from(values: list[tuple[Decimal, int]], percentile: Decimal, half_life: Decimal) -> dict:
    value = (weighted_quantile([(factor, weight(age, half_life)) for factor, age in values], percentile / 100)
             if len(values) >= MIN_MEALS else None)
    return {"count": len(values), "units_per_ke": value if value is not None and value > 0 else None}


SURCHARGE = "surcharge"
SURCHARGE_MGDL = Decimal(40)
SURCHARGE_FULL_GRAMS = Decimal(40)
METHODS = ("percentile", SURCHARGE)
# Options of the tuning rows; the configured value is always added.
TUNING_PERCENTILES = tuple(Decimal(value) for value in (25, 30, 35, 40, 45, 50))
TUNING_SURCHARGES = tuple(Decimal(value) for value in (0, 20, 40, 60, 80))
# Differences up to this many meals (or this share of the tested meals) count as chance.
NOISE_MEALS = 2
NOISE_SHARE = Decimal("0.05")


def surcharge_share(grams: Decimal) -> Decimal:
    """Share of the full meal surcharge: grows linearly with the carbs up to SURCHARGE_FULL_GRAMS."""
    return min(Decimal(1), max(Decimal(0), grams) / SURCHARGE_FULL_GRAMS)


def surcharge_units_per_ke(grams: Decimal, carbs_isf: Decimal, mgdl: Decimal = SURCHARGE_MGDL) -> Decimal:
    """The surcharge expressed in IE/KE of a meal with Σ[g × KF] = carbs_isf."""
    return 10 * mgdl * surcharge_share(grams) / carbs_isf


def base_factor(point: dict, mgdl: Decimal = SURCHARGE_MGDL) -> Decimal | None:
    """A meal's factor without its surcharge share; None without carbs or meal size."""
    grams, carbs_isf = point.get("carbs_g"), point.get("carbs_isf")
    if not grams or not carbs_isf or carbs_isf <= 0:
        return None
    return point["value"] - surcharge_units_per_ke(grams, carbs_isf, mgdl)


def base_history(points: list[dict], key: tuple, day: date, lookback: int,
                 mgdl: Decimal = SURCHARGE_MGDL) -> list[tuple[Decimal, int]]:
    """(factor without surcharge, age in days) of the same meal key before day."""
    result = []
    for point in points:
        age = (day - point["day"]).days
        if point["key"] == key and 1 <= age <= lookback:
            base = base_factor(point, mgdl)
            if base is not None:
                result.append((base, age))
    return result


def predict_method(points: list[dict], key: tuple, day: date, percentile: Decimal,
                   half_life: Decimal, lookback: int, method: str, mgdl: Decimal = SURCHARGE_MGDL) -> dict:
    """Like predict; with the surcharge method the factor is the percentile without surcharge."""
    if method != SURCHARGE:
        return predict(points, key, day, percentile, half_life, lookback)
    return predict_from(base_history(points, key, day, lookback, mgdl), percentile, half_life)


def median(values: list[Decimal]) -> Decimal | None:
    """Median of sorted values; None when empty."""
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def day_type_ratios(points: list[dict], today: date, days: int = RATIO_DAYS,
                    reference_days: int = RATIO_REFERENCE_DAYS, neighbours: int = RATIO_NEIGHBOURS) -> dict:
    """Stress surcharge of work days over free days at breakfast, over the last `days` days.
    Free days are the base: work = free × (1 + surcharge). Overall: median work factor / median free
    factor − 1. Per free-day meal: the median of the same meal kind on the `neighbours` nearest work
    days before and after it (at most `reference_days` away; needs two) divided by its factor − 1, so a
    stable surcharge shows up as tightly grouped single values. For information only."""
    result = {}
    for kind in ("breakfast",):
        recent = [point for point in points if point["key"][0] == kind and 0 <= (today - point["day"]).days < days]
        work = sorted(point["value"] for point in recent if point["day_type"] == "work")
        free = sorted(point["value"] for point in recent if point["day_type"] == "free")
        singles = []
        for point in sorted(recent, key=lambda item: item["at"]):
            if point["day_type"] != "free":
                continue
            work_days = sorted((item for item in points if item["key"][0] == kind and item["day_type"] == "work"),
                               key=lambda item: item["at"])
            before = [item for item in work_days if 1 <= (point["day"] - item["day"]).days <= reference_days]
            after = [item for item in work_days if 1 <= (item["day"] - point["day"]).days <= reference_days]
            reference = sorted(item["value"] for item in before[-neighbours:] + after[:neighbours])
            if len(reference) >= 2:
                singles.append({"day": point["day"].isoformat(), "surcharge": median(reference) / point["value"] - 1,
                                "before": len(before[-neighbours:]), "after": len(after[:neighbours])})
        surcharges = sorted(item["surcharge"] for item in singles)
        result[kind] = {
            "work_count": len(work), "work_median": median(work),
            "free_count": len(free), "free_median": median(free),
            "surcharge": median(work) / median(free) - 1 if work and free else None,
            "singles": singles, "single_median": median(surcharges),
            "single_min": surcharges[0] if surcharges else None, "single_max": surcharges[-1] if surcharges else None,
        }
    return {"days": days, "reference_days": reference_days, "neighbours": neighbours, "meals": result}


def predict_surcharge(values: list[tuple[Decimal, int]], percentile: Decimal, half_life: Decimal,
                      target: dict, mgdl: Decimal = SURCHARGE_MGDL) -> Decimal | None:
    """Full factor for the target meal: percentile without surcharge plus the meal's surcharge share."""
    base = predict_from(values, percentile, half_life)["units_per_ke"]
    if base is None or not target.get("carbs_g") or not target.get("carbs_isf"):
        return None
    return base + surcharge_units_per_ke(target["carbs_g"], target["carbs_isf"], mgdl)


def backtest(points: list[dict], since: date, variants: list[tuple],
             target_glucose: Decimal = Decimal(120), surcharge_percentile: Decimal = Decimal("37.5"),
             surcharge_mgdl: Decimal = SURCHARGE_MGDL) -> dict:
    """Simulate each (lookback, half-life, percentile or SURCHARGE) variant on the meals since `since`.
    Only meals every variant can predict count, so all cells compare the same meals.
    SURCHARGE uses `surcharge_percentile` on factors without `surcharge_mgdl` and adds the meal's own share.
    A variant (lookback, half-life, percentile, surcharge mg/dL) with a surcharge above 0 does the same
    at its own percentile and surcharge."""
    rows = []
    by_key: dict[tuple, list[dict]] = {}
    for point in points:
        by_key.setdefault(point["key"], []).append(point)
    specs = []
    for variant in variants:
        lookback, half_life, percentile = variant[:3]
        mgdl = variant[3] if len(variant) > 3 else None
        if percentile == SURCHARGE:
            percentile, mgdl = surcharge_percentile, surcharge_mgdl
        specs.append((lookback, half_life, percentile, mgdl if mgdl else None))
    longest = max((spec[0] for spec in specs), default=0)
    for target in sorted(points, key=lambda point: point["at"]):
        if target["day"] < since or target["value"] <= 0:
            continue
        # The history for the longest lookback once per meal (and surcharge); each variant takes its own slice.
        known = history(by_key[target["key"]], target["key"], target["day"], longest)
        known_base = {mgdl: base_history(by_key[target["key"]], target["key"], target["day"], longest, mgdl)
                      for mgdl in {spec[3] for spec in specs if spec[3]}}
        predictions = [
            predict_surcharge([item for item in known_base[mgdl] if item[1] <= lookback], percentile, half_life,
                              target, mgdl)
            if mgdl else
            predict_from([item for item in known if item[1] <= lookback], percentile, half_life)["units_per_ke"]
            for lookback, half_life, percentile, mgdl in specs]
        if None in predictions:
            continue
        glucose = None
        if target.get("carbs_isf"):
            # The dose help corrects to the target at the meal, so a dose with factor P ends at
            # target + (F − P) × Σ[g × KF] / 10 under the meal model; F already contains the measured
            # course, so the measured endpoint must not be added again. Only the factor error counts.
            glucose = [target_glucose + (target["value"] - value) * target["carbs_isf"] / 10
                       for value in predictions]
        rows.append({"units_per_ke": target["value"], "predictions": predictions, "glucose": glucose})
    stats = []
    for index in range(len(specs)):
        absolute = sorted(abs(row["predictions"][index] - row["units_per_ke"]) / row["units_per_ke"] for row in rows)
        levels = sorted(row["glucose"][index] for row in rows if row["glucose"] is not None)
        stats.append({
            "count": len(rows), "glucose_count": len(levels),
            "below": sum(level < GLUCOSE_LOW for level in levels),
            "in_range": sum(GLUCOSE_LOW <= level <= GLUCOSE_HIGH for level in levels),
            "above": sum(level > GLUCOSE_HIGH for level in levels),
            "mean_glucose": sum(levels) / len(levels) if levels else None,
            "median_glucose": median(levels),
            "median_abs_deviation": median(absolute),
        })
    return {"rows": rows, "stats": stats}


def tuning(points: list[dict], since: date, lookback: int, half_life: Decimal, percentile: Decimal,
           surcharge_mgdl: Decimal, target_glucose: Decimal = Decimal(120)) -> list[dict]:
    """One row per setting: every option of that setting with the other three as configured
    (surcharge 0 = plain percentile). Each row replays the meals since `since` that all its options
    can predict, so the lookback row (which needs 8 days of history) does not shrink the others."""
    rows = [
        ("percentile", sorted({*TUNING_PERCENTILES, percentile}), percentile,
         lambda value: (lookback, half_life, value, surcharge_mgdl)),
        ("surcharge", sorted({*TUNING_SURCHARGES, surcharge_mgdl}), surcharge_mgdl,
         lambda value: (lookback, half_life, percentile, value)),
        ("half_life", sorted({*HALF_LIVES, half_life}), half_life,
         lambda value: (lookback, value, percentile, surcharge_mgdl)),
        ("lookback", sorted({*LOOKBACKS, lookback}), lookback,
         lambda value: (value, half_life, percentile, surcharge_mgdl)),
    ]
    result = []
    for key, values, configured, variant in rows:
        stats = backtest(points, since, [variant(value) for value in values], target_glucose)["stats"]
        options = [{"value": value, "active": value == configured, **item} for value, item in zip(values, stats)]
        result.append({"key": key, "meal_count": options[0]["glucose_count"] if options else 0,
                       "options": options, **judge(options)})
    return result


def judge(options: list[dict]) -> dict:
    """Verdict of one tuning row. The best option has the most endpoints in range, then the fewest
    below. It is 'better' when it beats the configured option by more than chance; otherwise a
    spread of the lows or highs beyond chance means the setting trades lows against highs ('shift');
    else the setting makes no difference ('none')."""
    total = options[0]["glucose_count"] if options else 0
    noise = max(Decimal(NOISE_MEALS), NOISE_SHARE * total)
    best = None
    for option in options:
        option["best"] = False
        if best is None or (option["in_range"], -option["below"]) > (best["in_range"], -best["below"]):
            best = option
    active = next((option for option in options if option["active"]), None)
    if not total or best is None or active is None:
        return {"verdict": "none", "noise_meals": noise}
    best["best"] = True
    spread = lambda key: max(option[key] for option in options) - min(option[key] for option in options)
    if best["in_range"] - active["in_range"] > noise:
        verdict = "better"
    elif spread("below") > noise or spread("above") > noise:
        verdict = "shift"
    else:
        verdict = "none"
    return {"verdict": verdict, "noise_meals": noise}


def explain(points: list[dict], key: tuple, day: date, lookback: int, half_life: Decimal,
            percentile: Decimal, surcharge_mgdl: Decimal) -> dict:
    """The configured suggestion for a typical meal of `key`, step by step: the meals it uses with
    their weight, the weighted percentile (of factors without surcharge), and the dose for the
    median meal size of those meals at their median KF (Σ[g × KF] / g): KE × factor + surcharge IE."""
    used = sorted((point for point in points
                   if point["key"] == key and 1 <= (day - point["day"]).days <= lookback),
                  key=lambda point: point["at"], reverse=True)
    if surcharge_mgdl:
        used = [point for point in used if base_factor(point, surcharge_mgdl) is not None]
    meals = [{"day": point["day"].isoformat(), "age_days": (day - point["day"]).days, "units_per_ke": point["value"],
              "base_units_per_ke": base_factor(point, surcharge_mgdl) if surcharge_mgdl else point["value"],
              "weight": weight((day - point["day"]).days, half_life)} for point in used]
    factor = predict_from([(meal["base_units_per_ke"], meal["age_days"]) for meal in meals],
                          percentile, half_life)["units_per_ke"]
    sized = [point for point in used if point.get("carbs_g") and point.get("carbs_isf")]
    grams = median(sorted(point["carbs_g"] for point in sized))
    isf = median(sorted(point["carbs_isf"] / point["carbs_g"] for point in sized))
    carb_units = surcharge_units = None
    if factor is not None and grams is not None:
        carb_units = grams / 10 * factor
        surcharge_units = surcharge_mgdl * surcharge_share(grams) / isf if surcharge_mgdl else Decimal(0)
    return {
        "meal": key[0], "day_type": key[1], "count": len(meals), "min_meals": MIN_MEALS,
        "units_per_ke": factor, "carbs_g": grams, "isf": isf,
        "carb_units": carb_units, "surcharge_units": surcharge_units,
        "total_units": carb_units + surcharge_units if carb_units is not None else None,
    }


def tradeoffs(points: list[dict], since: date, lookback: int, half_life: Decimal, percentile: Decimal,
              surcharge_mgdl: Decimal, target_glucose: Decimal = Decimal(120)) -> dict:
    """Every combination of the tuning options, replayed on the same meals as the configured one.
    Lows and highs pull against each other, so there is no single best strategy: the trade-off line
    holds the combinations no other one beats on both fewer endpoints below and fewer above the range.
    Combinations with the same counts are represented by the one that changes the fewest settings."""
    configured = (lookback, half_life, percentile, surcharge_mgdl)
    variants = [(days, strength, value, mgdl)
                for days in sorted({*LOOKBACKS, lookback})
                for strength in sorted({*HALF_LIVES, half_life})
                for value in sorted({*TUNING_PERCENTILES, percentile})
                for mgdl in sorted({*TUNING_SURCHARGES, surcharge_mgdl})]
    stats = backtest(points, since, variants, target_glucose)["stats"]
    changes = lambda variant: [name for name, value, current in zip(
        ("lookback", "half_life", "percentile", "surcharge"), variant, configured) if value != current]
    options = {}
    for variant, item in zip(variants, stats):
        counts = (item["below"], item["above"])
        option = {"lookback_days": variant[0], "half_life_days": variant[1], "percentile": variant[2],
                  "surcharge_mgdl": variant[3], "below": item["below"], "in_range": item["in_range"],
                  "above": item["above"], "median_abs_deviation": item["median_abs_deviation"],
                  "changes": changes(variant), "variants": 1}
        known = options.get(counts)
        if known is None or len(option["changes"]) < len(known["changes"]):
            option["variants"] = known["variants"] + 1 if known else 1
            options[counts] = option
        else:
            known["variants"] += 1
    current = next(option for counts, option in options.items()
                   if counts == (stats[variants.index(configured)]["below"], stats[variants.index(configured)]["above"]))
    current = {**current, "changes": []} if current["changes"] else current
    line = sorted((option for option in options.values()
                   if not any(other["below"] <= option["below"] and other["above"] <= option["above"]
                              and (other["below"], other["above"]) != (option["below"], option["above"])
                              for other in options.values())),
                  key=lambda option: option["below"])
    for option in line:
        option["delta_below"] = option["below"] - current["below"]
        option["delta_above"] = option["above"] - current["above"]
    meals = stats[0]["glucose_count"] if stats else 0
    return {"meal_count": meals, "current": current, "line": line,
            "cloud": [{"below": option["below"], "above": option["above"], "variants": option["variants"]}
                      for option in options.values()]}
