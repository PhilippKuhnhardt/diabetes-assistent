"""A correction without food as a scale by how fast the last main meal rose (user request and approval 2026-11-15).

Analysis in the project files, analysen/korrektur-skala-2026-11-15.md (21.09.-07.11., 51 of Philipp's corrections and
74 "BZ prüfen" moments, glucose without his corrections recomputed with the app's KF and insulin curve): after a
flat rise the dose help's correction was too small (median +1.8 IE), after a steep one too large (-0.6 IE); a steep
rise is fast carbs, not more of them, and falls steeply again. The spread per meal is far larger than that
effect: over all 122 moments the right correction was in the median 0.96 x the dose help's, with quartiles
0.55 and 1.45 (user request 2026-11-15: middle = most likely, the ends grounded in what corrections with
little / much need would have needed). So the app shows x0.6 / x1 / x1.4 and only marks the end the rise leans
to (flat: median 1.45, steep: median 0.85); Philipp picks.
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
    if correction <= 0:
        return None
    start, isf = Decimal(result["forecast_mgdl"]), Decimal(result["isf_mgdl_per_unit"])
    out = {"meal_at": rise["meal_at"].isoformat(), "tempo": rise["tempo"],
           "slope_mgdl_per_min": str(rise["slope_mgdl_per_min"]) if rise["slope_mgdl_per_min"] is not None else None}
    # The low end is at least FAST_MIN_GAP below normal: late corrections are small while the right one still
    # spreads about +-1 IE (3-4 h after the meal only 1 in 5 lay within x0.6..x1.4). The high end gets no such
    # gap: there it cost lows (late < 70: 38 % at x1.4, 47 % with +1 IE; user decision 2026-11-15).
    ends = {"fast": min(correction * FAST_FACTOR, correction - FAST_MIN_GAP), "slow": correction * SLOW_FACTOR}
    for name, value in ends.items():
        units = half_units(max(Decimal(0), value))
        out[name + "_entry_units"] = str(units)
        out[name + "_expected_glucose_mgdl"] = str(start - units * isf)
    return out

