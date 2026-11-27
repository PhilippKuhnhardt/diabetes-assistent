"""Dishes: what a meal was, so recurring meals can be traced over time.

A diary carb entry can name one dish from the user's catalogue (e.g. Porridge, Canteen pasta). A dish only
labels entries; it changes no dose. Its history shows how the dish turned out: the meal factor of
its main meals against the usual factor of that meal kind at the time (`ratio`; 1.2 = the meal
needed 20 % more insulin than usual, so it behaved like 20 % more carbs).

Names, categories and descriptions are checked here; the outcome rules live in the core."""
from __future__ import annotations

from app import core
from app.i18n import tr

NAME_MAX = 60
CATEGORY_MAX = 40
DESCRIPTION_MAX = 1000
# The outcome rules (reference factor, ratio, spread, typical carbs, the hint) are in the Rust core
# (core/diabetes-core/src/dishes.rs); these functions call it.


def validate_name(value) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    if not name:
        raise ValueError(tr("Bitte einen Namen für das Gericht eingeben"))
    return name


def validate_category(value) -> str:
    return " ".join(str(value or "").split())[:CATEGORY_MAX]


def validate_description(value) -> str:
    return str(value or "").strip()[:DESCRIPTION_MAX]


def typical_carbs(carbs: list[float]) -> int | None:
    """Carbs a new entry of the dish starts with: the median of its last entries (oldest first)."""
    return core.call("dishes.typical_carbs", carbs=[float(value) if value is not None else None for value in carbs])


def reference_factor(meals: list[dict], meal: dict) -> float | None:
    """The usual factor when the meal was eaten: median factor of the valid, not excluded main meals
    of its kind in the REFERENCE_DAYS before its day (the meal itself and its day stay out)."""
    return core.call("dishes.reference_factor", meals=meals, meal=meal)


def with_ratio(meals: list[dict], meal: dict) -> dict:
    """The main meal with the usual factor of its kind then and its ratio to it."""
    return core.call("dishes.with_ratio", meals=meals, meal=meal)


def history(meals: list[dict], dish_id: int) -> list[dict]:
    """The dish's main meals, oldest first, each with the usual factor then and its ratio to it."""
    return core.call("dishes.history", meals=meals, dish_id=dish_id)


def outcome(rows: list[dict], typical: float | None) -> dict:
    """How a dish turned out over its main meals (history rows): how many, the ratio to the usual
    factor with its spread, and the carbs the typical entry behaved like."""
    return core.call("dishes.outcome", rows=rows, typical=typical)


def hint(count: int, typical: float | None, result: dict | None = None) -> str:
    """One line on a dish for the entry form and the catalogue."""
    return core.call("dishes.hint", count=count, typical=typical, result=result)


def catalogue(meals: list[dict], dish_ids: list[int], records: list[dict]) -> dict[int, dict]:
    """Per dish: count, typical_carbs, last_at, outcome, hint and its latest main meals (newest first)."""
    result = core.call("dishes.catalogue", meals=meals, dish_ids=dish_ids, records=records)
    return {row.pop("id"): row for row in result}
