"""Dishes: what a meal was, so recurring meals can be traced over time (user request 2026-11-16).

A diary carb entry can name one dish from Philipp's catalogue (Porridge, Kantine Pasta). A dish only
labels entries; it changes no dose. Its history shows how the dish turned out: the meal factor of
its main meals against the usual factor of that meal kind at the time (`ratio`; 1.2 = the meal
needed 20 % more insulin than usual, so it behaved like 20 % more carbs)."""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

NAME_MAX = 60
CATEGORY_MAX = 40
DESCRIPTION_MAX = 1000
# The usual factor of a meal kind: the median of its valid meals in this many days before the meal.
REFERENCE_DAYS = 30
REFERENCE_MIN_MEALS = 4
# A dish's ratio is named from this many valid main meals on.
RATIO_MIN_MEALS = 3
# A new entry's suggested carbs: the median of the dish's last entries.
TYPICAL_ENTRIES = 5


def validate_name(value) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    if not name:
        raise ValueError("Bitte einen Namen für das Gericht eingeben")
    return name


def validate_category(value) -> str:
    return " ".join(str(value or "").split())[:CATEGORY_MAX]


def validate_description(value) -> str:
    return str(value or "").strip()[:DESCRIPTION_MAX]


def typical_carbs(carbs: list[float]) -> float | None:
    """Carbs a new entry of the dish starts with: the median of its last entries (oldest first)."""
    recent = [value for value in carbs if value and value > 0][-TYPICAL_ENTRIES:]
    return round(median(recent)) if recent else None


def reference_factor(meals: list[dict], meal: dict) -> float | None:
    """The usual factor when the meal was eaten: median factor of the valid, not excluded main meals
    of its kind in the REFERENCE_DAYS before its day (the meal itself and its day stay out)."""
    day = date.fromisoformat(meal["day"])
    first = (day - timedelta(days=REFERENCE_DAYS)).isoformat()
    values = [float(other["factor"]) for other in meals
              if other["meal"] == meal["meal"] and other["factor"] is not None and not other["excluded"]
              and first <= other["day"] < meal["day"]]
    return median(values) if len(values) >= REFERENCE_MIN_MEALS else None


def _spread(values: list[float]) -> tuple[float, float]:
    """The middle half of the values (quartiles) from 4 values on, else their range."""
    ordered = sorted(values)
    if len(ordered) < 4:
        return ordered[0], ordered[-1]
    def quantile(share: float) -> float:
        position = (len(ordered) - 1) * share
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return quantile(0.25), quantile(0.75)


def history(meals: list[dict], dish_id: int) -> list[dict]:
    """The dish's main meals, oldest first, each with the usual factor then and its ratio to it."""
    rows = []
    for meal in meals:
        if meal.get("dish_id") != dish_id:
            continue
        reference = reference_factor(meals, meal)
        valid = meal["factor"] is not None and not meal["excluded"]
        rows.append({**meal, "reference_factor": round(reference, 3) if reference else None,
                     "ratio": round(float(meal["factor"]) / reference, 3) if valid and reference else None})
    return sorted(rows, key=lambda row: row["at"])


def outcome(rows: list[dict], typical: float | None) -> dict:
    """How a dish turned out over its main meals (history rows): how many, the ratio to the usual
    factor with its spread, and the carbs the typical entry behaved like."""
    ratios = [row["ratio"] for row in rows if row["ratio"] is not None]
    deltas = [float(row["delta_mgdl"]) for row in rows if row["delta_mgdl"] is not None and not row["excluded"]]
    ends = [float(row["g_end_mgdl"]) for row in rows if row["g_end_mgdl"] is not None and not row["excluded"]]
    result = {"meals": len(rows), "valid": len(ratios), "ratio": None, "ratio_low": None, "ratio_high": None,
              "effective_carbs": None, "effective_low": None, "effective_high": None,
              "delta_mgdl": round(median(deltas)) if deltas else None,
              "end_mgdl": round(median(ends)) if ends else None}
    if len(ratios) >= RATIO_MIN_MEALS:
        low, high = _spread(ratios)
        middle = median(ratios)
        result.update({"ratio": round(middle, 2), "ratio_low": round(low, 2), "ratio_high": round(high, 2)})
        if typical:
            result.update({"effective_carbs": round(typical * middle), "effective_low": round(typical * low),
                           "effective_high": round(typical * high)})
    return result


def hint(count: int, typical: float | None, result: dict | None = None) -> str:
    """One line on a dish for the entry form and the catalogue."""
    parts = [f"{count}× eingetragen" if count else "noch nie eingetragen"]
    if typical:
        parts.append(f"meist {typical:g} g")
    if result and result.get("ratio") is not None:
        percent = round((result["ratio"] - 1) * 100)
        need = "wie üblich" if abs(percent) < 5 else f"{abs(percent)} % {'mehr' if percent > 0 else 'weniger'} Insulin als üblich"
        text = f"brauchte {need} ({result['valid']} Mahlzeiten)"
        if typical and abs(percent) >= 5:
            text += f", verhielt sich wie {result['effective_low']}–{result['effective_high']} g"
        parts.append(text)
    return ", ".join(parts)
