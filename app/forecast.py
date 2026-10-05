"""Suggested meal factor (Automatik) from recent calculated meal factors.

Three methods can be compared:
- flat: percentile of the past 28 days, every meal weighted equally (the original rule).
- weighted: the same percentile, but each meal weighted by 0.5 ** (age in days / half-life).
  A percentile depends on the order of values, not their size, so one odd day can move it
  by at most its weight share, however extreme it is.
- weighted_shift: weighted, multiplied by a day shift. The shift only applies when the last
  three finished meals of any kind (within 48 h) all lay on the same side of their own weighted
  median; it then uses the smallest of the three deviations, capped at ±15 %.

The backtest recomputes, for every past meal, what each method would have suggested from the
meals before it, so the methods can be compared against the factor that was actually calculated,
and estimates where the endpoint glucose would have landed with that factor (below, inside or
above 80–160 mg/dL).
These are model choices for experimentation, not dosing advice.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

METHODS = ("flat", "weighted", "weighted_shift")
LOOKBACK_DAYS = 28
MIN_MEALS = 4
SHIFT_MEALS = 3
SHIFT_HOURS = 48
SHIFT_CAP = Decimal("0.15")
HIT_TOLERANCE = Decimal("0.10")
HALF = Decimal("0.5")
# Range for the endpoint glucose three hours after a meal; deliberately tighter than the
# Nightscout target range (80–180), which also covers the post-meal rise.
GLUCOSE_LOW = Decimal(80)
GLUCOSE_HIGH = Decimal(160)


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


def history(points: list[dict], key: tuple, day: date) -> list[tuple[Decimal, int]]:
    """(factor, age in days) of the same meal key from the 28 days before day; today is left out."""
    result = []
    for point in points:
        age = (day - point["day"]).days
        if point["key"] == key and 1 <= age <= LOOKBACK_DAYS:
            result.append((point["value"], age))
    return result


def weight(age: int, half_life: Decimal) -> Decimal:
    return HALF ** (Decimal(age) / half_life)


def baseline(points: list[dict], key: tuple, day: date, fraction: Decimal,
             method: str, half_life: Decimal) -> Decimal | None:
    values = history(points, key, day)
    if len(values) < MIN_MEALS:
        return None
    if method == "flat":
        return weighted_quantile([(value, Decimal(1)) for value, _ in values], fraction)
    return weighted_quantile([(value, weight(age, half_life)) for value, age in values], fraction)


def day_shift(points: list[dict], at: datetime, half_life: Decimal) -> dict:
    """Shift from the last three finished meals: all above (or all below) their own weighted median."""
    finished = sorted((point for point in points
                       if point.get("end") is not None and point["end"] <= at
                       and at - point["at"] <= timedelta(hours=SHIFT_HOURS)),
                      key=lambda point: point["at"], reverse=True)[:SHIFT_MEALS]
    meals = []
    for point in finished:
        reference = baseline(points, point["key"], point["day"], HALF, "weighted", half_life)
        meals.append({
            "id": point["id"], "meal": point["key"][0], "day_type": point["key"][1], "at": point["at"],
            "units_per_ke": point["value"], "reference_units_per_ke": reference,
            "ratio": point["value"] / reference if reference and reference > 0 else None,
        })
    result = {"factor": Decimal(1), "applied": False, "meals": meals}
    ratios = [meal["ratio"] for meal in meals]
    if len(meals) < SHIFT_MEALS or None in ratios:
        result["reason"] = "too_few"
    elif all(ratio > 1 for ratio in ratios) or all(ratio < 1 for ratio in ratios):
        smallest = min(ratios, key=lambda ratio: abs(ratio - 1))
        result["factor"] = min(max(smallest, 1 - SHIFT_CAP), 1 + SHIFT_CAP)
        result["applied"] = True
        result["reason"] = "higher" if smallest > 1 else "lower"
    else:
        result["reason"] = "mixed"
    return result


def predict(points: list[dict], key: tuple, at: datetime, day: date, percentile: Decimal,
            method: str, half_life: Decimal) -> dict:
    fraction = percentile / 100
    count = len(history(points, key, day))
    base = baseline(points, key, day, fraction, method, half_life)
    shift = day_shift(points, at, half_life) if method == "weighted_shift" else None
    value = base * shift["factor"] if base is not None and shift else base
    return {
        "method": method, "count": count, "baseline_units_per_ke": base,
        "units_per_ke": value if value is not None and value > 0 else None, "shift": shift,
    }


def backtest(points: list[dict], since: date, percentile: Decimal, half_life: Decimal) -> dict:
    """Compare what each method would have suggested before each meal with its calculated factor."""
    rows = []
    for target in sorted(points, key=lambda point: point["at"]):
        if target["day"] < since or target["value"] <= 0:
            continue
        predictions = {method: predict(points, target["key"], target["at"], target["day"],
                                       percentile, method, half_life)["units_per_ke"]
                       for method in METHODS}
        if any(value is None for value in predictions.values()):
            continue
        row = {"id": target["id"], "at": target["at"], "meal": target["key"][0],
               "day_type": target["key"][1], "units_per_ke": target["value"],
               "predictions": predictions, "end_glucose": target.get("end_glucose"), "glucose": None}
        if target.get("end_glucose") is not None and target.get("carbs_isf"):
            # Under the meal model, (P − F) IE/KE more for Σ[g × KF] / 10 shifts the endpoint
            # by −(P − F) × Σ[g × KF] / 10 mg/dL; all other doses stay as they were.
            row["glucose"] = {method: target["end_glucose"] - (value - target["value"]) * target["carbs_isf"] / 10
                              for method, value in predictions.items()}
        rows.append(row)
    stats = {}
    for method in METHODS:
        deviations = sorted((row["predictions"][method] - row["units_per_ke"]) / row["units_per_ke"]
                            for row in rows)
        absolute = sorted(abs(value) for value in deviations)
        stats[method] = {
            "count": len(rows),
            "hits": sum(abs(value) <= HIT_TOLERANCE for value in deviations),
            "too_much": sum(value > HIT_TOLERANCE for value in deviations),
            "too_little": sum(value < -HIT_TOLERANCE for value in deviations),
            "glucose_count": sum(row["glucose"] is not None for row in rows),
            "below": sum(row["glucose"] is not None and row["glucose"][method] < GLUCOSE_LOW
                         for row in rows),
            "in_range": sum(row["glucose"] is not None
                            and GLUCOSE_LOW <= row["glucose"][method] <= GLUCOSE_HIGH for row in rows),
            "above": sum(row["glucose"] is not None and row["glucose"][method] > GLUCOSE_HIGH
                         for row in rows),
            "median_abs_deviation": (absolute[len(absolute) // 2] if len(absolute) % 2 else
                                     (absolute[len(absolute) // 2 - 1] + absolute[len(absolute) // 2]) / 2)
                                    if absolute else None,
        }
    return {"rows": rows, "stats": stats}
