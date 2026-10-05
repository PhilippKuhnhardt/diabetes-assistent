"""Suggested meal factor (Automatik) from recent calculated meal factors.

The suggestion is a percentile of the meal factors of the same meal kind from the last
`lookback` days (14, 21 or 28), each meal weighted by 0.5 ** (age in days / half-life).
A percentile depends on the order of values, not their size, so one odd day can move it
by at most its weight share, however extreme it is.

The backtest recomputes, for every past meal, what each combination of lookback, half-life and
percentile would have suggested from the meals before it, and estimates where the endpoint
glucose would have landed with that factor (below, inside or above 80–160 mg/dL).
These are model choices for experimentation, not dosing advice.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache

LOOKBACKS = (14, 21, 28)
MAX_LOOKBACK = max(LOOKBACKS)
MIN_MEALS = 4
HALF = Decimal("0.5")
# Half-lives compared in the backtest: four stronger weightings, the default and one weaker.
HALF_LIVES = (Decimal(1), Decimal(2), Decimal(3), Decimal(5), Decimal(7), Decimal(14))
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
    values = history(points, key, day, lookback)
    value = (weighted_quantile([(factor, weight(age, half_life)) for factor, age in values], percentile / 100)
             if len(values) >= MIN_MEALS else None)
    return {"count": len(values), "units_per_ke": value if value is not None and value > 0 else None}


def median(values: list[Decimal]) -> Decimal | None:
    """Median of sorted values; None when empty."""
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def backtest(points: list[dict], since: date, variants: list[tuple[int, Decimal, Decimal]]) -> dict:
    """Simulate each (lookback, half-life, percentile) variant on the meals since `since`.
    Only meals every variant can predict count, so all cells compare the same meals."""
    rows = []
    for target in sorted(points, key=lambda point: point["at"]):
        if target["day"] < since or target["value"] <= 0:
            continue
        predictions = [predict(points, target["key"], target["day"], percentile, half_life, lookback)["units_per_ke"]
                       for lookback, half_life, percentile in variants]
        if None in predictions:
            continue
        glucose = None
        if target.get("end_glucose") is not None and target.get("carbs_isf"):
            # Under the meal model, (P − F) IE/KE more for Σ[g × KF] / 10 shifts the endpoint
            # by −(P − F) × Σ[g × KF] / 10 mg/dL; all other doses stay as they were.
            glucose = [target["end_glucose"] - (value - target["value"]) * target["carbs_isf"] / 10
                       for value in predictions]
        rows.append({"units_per_ke": target["value"], "predictions": predictions, "glucose": glucose})
    stats = []
    for index in range(len(variants)):
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
