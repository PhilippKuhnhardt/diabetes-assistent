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
    meals = [item for item in treatments if on_board.is_main_meal(item) and item["at"] <= now]
    if not meals:
        return None
    meal = max(meals, key=lambda item: item["at"])
    minutes = (now - meal["at"]).total_seconds() / 60
    if not RISE_MINUTES <= minutes <= TO_MINUTES:
        return None
    read = calculation.glucose_interpolator([entry for entry in entries if entry["at"] <= now])
    steps = RISE_MINUTES // STEP_MINUTES
    values = []
    for index in range(steps + 2):
        try:
            values.append(read(meal["at"] + timedelta(minutes=STEP_MINUTES * index))["value"])
        except ValueError:
            values.append(None)
    smooth = []
    for index in range(steps + 1):
        near = [value for value in values[max(0, index - 1):index + 2] if value is not None]
        smooth.append(sum(near) / len(near) if near and values[index] is not None else None)
    back = SLOPE_MINUTES // STEP_MINUTES
    slopes = [(smooth[index] - smooth[index - back]) / SLOPE_MINUTES for index in range(back, steps + 1)
              if smooth[index] is not None and smooth[index - back] is not None]
    slope = max(slopes) if len(slopes) >= 3 else None
    if slope is None:
        # Without enough CGM the "Schnell" mark of the meal stands in.
        tempo = "fast" if meal.get("fast") else None
    else:
        tempo = "fast" if slope > STEEP_MGDL_PER_MIN else "slow" if slope < FLAT_MGDL_PER_MIN else "normal"
    return {"meal_at": meal["at"], "slope_mgdl_per_min": slope, "tempo": tempo}


def scale(result: dict, rise: dict | None) -> dict | None:
    """Fast and slow corrections next to the normal one of a dosing.suggest result without carbs, or None
    without a recent meal or when the normal correction is not positive."""
    if rise is None or result.get("forecast_mgdl") is None or result.get("correction_units") is None:
        return None
    correction = Decimal(result["correction_units"])
    # Only when normal suggests an injectable dose: a correction rounding to 0 IE gave three zeros.
    if correction <= 0 or Decimal(result.get("entry_units") or 0) <= 0:
        return None
    start, isf = Decimal(result["forecast_mgdl"]), Decimal(result["isf_mgdl_per_unit"])
    out = {"meal_at": rise["meal_at"].isoformat(), "tempo": rise["tempo"],
           "slope_mgdl_per_min": str(rise["slope_mgdl_per_min"]) if rise["slope_mgdl_per_min"] is not None else None}
    # The low end is at least FAST_MIN_GAP below normal: late corrections are small while the right one still
    # spreads about +-1 IE (3-4 h after the meal only 1 in 5 lay within x0.6..x1.4). The high end gets no such
    # gap: there it cost lows.
    ends = {"fast": min(correction * FAST_FACTOR, correction - FAST_MIN_GAP), "slow": correction * SLOW_FACTOR}
    for name, value in ends.items():
        units = half_units(max(Decimal(0), value))
        out[name + "_entry_units"] = str(units)
        out[name + "_expected_glucose_mgdl"] = str(start - units * isf)
    return out



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
    low_factor, high_factor = (FAST_FACTOR, SLOW_FACTOR) if correction else (MEAL_LOW_FACTOR, MEAL_HIGH_FACTOR)
    low = half_units(max(Decimal(0), min(normal_units * low_factor, normal_units - FAST_MIN_GAP)))
    high = max(half_units(normal_units * high_factor), normal_units + SLIDER_MIN_SPAN)
    recommended = {"fast": low, "slow": high}.get(tempo, normal_units) if correction else normal_units
    corridor = CORRIDOR_MGDL[tempo if correction else "meal"]
    return {"min_units": str(low), "max_units": str(high), "recommended_units": str(recommended),
            "normal_units": str(normal_units), "tempo": tempo if correction else None,
            "corridor_mgdl": [str(value) for value in corridor]}
