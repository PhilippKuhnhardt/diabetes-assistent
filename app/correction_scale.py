"""A correction without food as a scale by how fast the last main meal rose.

Personal analysis (21.09.-07.11., 51 corrections and
74 "BZ prüfen" moments, glucose without the corrections recomputed with the app's KF and insulin curve): after a
flat rise the dose help's correction was too small (median +1.8 IE), after a steep one too large (-0.6 IE); a steep
rise is fast carbs, not more of them, and falls steeply again. The spread per meal is far larger than that
effect: over all 122 moments the right correction was in the median 0.96 x the dose help's, with quartiles
0.55 and 1.45 (middle = most likely, the ends grounded in what corrections with
little / much need would have needed). So the app shows x0.6 / x1 / x1.4 and only marks the end the rise leans
to (flat: median 1.45, steep: median 0.85); the user picks.
The rise is the steepest 15-min slope of the smoothed CGM within the first hour after the meal (the analysis'
tertiles: below 1.0 flat, above about 2.0 steep).
"""
from __future__ import annotations

from app import core
from datetime import datetime, timedelta
from decimal import Decimal
from app import calculation, on_board
from app.dosing import half_units


FAST_FACTOR, SLOW_FACTOR = Decimal("0.6"), Decimal("1.4")
FAST_MIN_GAP = Decimal(1)  # IE
STEEP_MGDL_PER_MIN, FLAT_MGDL_PER_MIN = Decimal("2.0"), Decimal("1.0")
RISE_MINUTES = 60          # the slope is measured over the first hour after the meal start
SLOPE_MINUTES = 15
STEP_MINUTES = 5
TO_MINUTES = 450           # a meal counts until 7.5 h after its start, as the "BZ prüfen" reminder


def meal_rise(entries: list[dict], treatments: list[dict], now: datetime) -> dict | None:
    """The latest main meal 1 h to 7.5 h ago and how steeply glucose rose after it:
    {"meal_at", "slope_mgdl_per_min" (None with too many CGM gaps), "tempo": fast / normal / slow / None}."""
    return core.call("correction_scale.meal_rise", entries=entries, treatments=treatments, now=now)


def scale(result: dict, rise: dict | None) -> dict | None:
    """Fast and slow corrections next to the normal one of a dosing.suggest result without carbs, or None
    without a recent meal or when the normal correction is not positive."""
    return core.call("correction_scale.scale", result=result, rise=rise)


# The dose slider: one scale for every dose, from little to much need, with the
# recommendation marked and where glucose likely ends. Lower / cautious options were dropped for it.
MEAL_LOW_FACTOR, MEAL_HIGH_FACTOR = Decimal("0.75"), Decimal("1.2")
SLIDER_MIN_SPAN = Decimal("0.5")
# Where glucose ended about 4 h later, relative to the dose help's expected value, as 10/25/50/75/90 %
# (mg/dL). Corrections: 122 corrections and "BZ prüfen" moments 21.09.-07.11. at the dose help's dose,
# by the rise after the meal. Meals: 117 main meals,
# untreated glucose 4 h after the meal minus the expectation from the entries at the meal.
CORRIDOR_MGDL = {
    "fast": (-65, -41, -18, 31, 90),
    "normal": (-72, -31, -11, 31, 64),
    "slow": (-70, -6, 20, 65, 97),
    None: (-73, -36, -4, 50, 80),
    "meal": (-89, -44, -8, 33, 79),
}


def dose_range(normal_units: Decimal, correction: bool, tempo: str | None = None) -> dict:
    """Slider ends and the recommended dose around the dose help's normal dose (all in 0.5 IE).
    Correction: x0.6 but at least 1 IE less, x1.4 (what was right after meals, quartiles 0.55 / 1.45);
    the rise after the meal moves the recommendation to the matching end. Meal: x0.75 but at least 1 IE
    less, x1.2 (quartiles of what the meal needed against the dose given: 0.75 / 1.16)."""
    return core.call("correction_scale.dose_range", normal_units=normal_units, correction=correction, tempo=tempo)
