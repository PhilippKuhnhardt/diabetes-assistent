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
A meal marked fat/protein carries FAT_SURCHARGE_MGDL more (setting auto_fat_surcharge_mgdl): fat meals no longer pull up the
factor of the others, so non-fat meals (e.g. in the canteen) get less and fat ones more.

Place factor: for a place switched on for it, the dose of a main meal there is scaled by the
weighted median of actual ÷ suggested factor of the main meals eaten there in the last PLACE_LOOKBACK_DAYS
(at least PLACE_MIN_MEALS, half-life PLACE_HALF_LIFE, clamped to PLACE_SCALE_MIN..PLACE_SCALE_MAX). Each past
suggestion is recomputed as the Automatik would have given it on that day, without any place factor.
Meals at such a place (`own_place`) stay out of every history the Automatik draws on (user question 2026-11-06):
otherwise the canteen's lower factors would pull down the factor of all other meals and, measured against that
lowered factor, shrink their own place factor again. The base factor comes from the other meals only, and the
place factor is measured against it.

The tuning rows vary one setting at a time around the configured ones (percentile, surcharge,
half-life, lookback), each replayed on the largest set of meals all its options can predict.
These are model choices for experimentation, not dosing advice.
"""
from __future__ import annotations

from app import core
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
    return core.call("forecast.weighted_quantile", pairs=pairs, fraction=fraction)


def history(points: list[dict], key: tuple, day: date, lookback: int) -> list[tuple[Decimal, int]]:
    """(factor, age in days) of the same meal key from the lookback days before day; today is left out."""
    return core.call("forecast.history", points=points, key=key, day=day, lookback=lookback)


@lru_cache(maxsize=1024)
def weight(age: int, half_life: Decimal) -> Decimal:
    return core.call("forecast.weight", age=age, half_life=half_life)


def predict(points: list[dict], key: tuple, day: date, percentile: Decimal,
            half_life: Decimal, lookback: int) -> dict:
    """Weighted percentile of the meal key's factors before day; None below MIN_MEALS meals."""
    return core.call(
        "forecast.predict",
        points=points, key=key, day=day, percentile=percentile, half_life=half_life, lookback=lookback)


SURCHARGE = "surcharge"
SURCHARGE_MGDL = Decimal(40)
SURCHARGE_FULL_GRAMS = Decimal(40)
FAT_SURCHARGE_MGDL = Decimal(120)
PLACE_LOOKBACK_DAYS = 60
PLACE_MIN_MEALS = 3
PLACE_HALF_LIFE = Decimal(14)
PLACE_SCALE_MIN, PLACE_SCALE_MAX = Decimal("0.5"), Decimal("1.5")
METHODS = ("percentile", SURCHARGE)
# Options of the tuning rows; the configured value is always added.
TUNING_PERCENTILES = tuple(Decimal(value) for value in (25, 30, 35, 40, 45, 50))
TUNING_SURCHARGES = tuple(Decimal(value) for value in (0, 20, 40, 60, 80))
# Differences up to this many meals (or this share of the tested meals) count as chance.
NOISE_MEALS = 2
NOISE_SHARE = Decimal("0.05")


def surcharge_share(grams: Decimal) -> Decimal:
    """Share of the full meal surcharge: grows linearly with the carbs up to SURCHARGE_FULL_GRAMS."""
    return core.call("forecast.surcharge_share", grams=grams)


def surcharge_units_per_ke(grams: Decimal, carbs_isf: Decimal, mgdl: Decimal = SURCHARGE_MGDL) -> Decimal:
    """The surcharge expressed in IE/KE of a meal with Σ[g × KF] = carbs_isf."""
    return core.call("forecast.surcharge_units_per_ke", grams=grams, carbs_isf=carbs_isf, mgdl=mgdl)


def base_factor(point: dict, mgdl: Decimal = SURCHARGE_MGDL, fat_mgdl: Decimal = Decimal(0)) -> Decimal | None:
    """A meal's factor without its surcharge share; None without carbs or meal size."""
    return core.call("forecast.base_factor", point=point, mgdl=mgdl, fat_mgdl=fat_mgdl)


def base_history(points: list[dict], key: tuple, day: date, lookback: int,
                 mgdl: Decimal = SURCHARGE_MGDL, fat_mgdl: Decimal = Decimal(0)) -> list[tuple[Decimal, int]]:
    """(factor without surcharge, age in days) of the same meal key before day."""
    return core.call("forecast.base_history", points=points, key=key, day=day, lookback=lookback, mgdl=mgdl,
                     fat_mgdl=fat_mgdl)


def predict_method(points: list[dict], key: tuple, day: date, percentile: Decimal,
                   half_life: Decimal, lookback: int, method: str, mgdl: Decimal = SURCHARGE_MGDL,
                   fat_mgdl: Decimal = Decimal(0)) -> dict:
    """Like predict; with the surcharge method the factor is the percentile without surcharge."""
    return core.call(
        "forecast.predict_method",
        points=points, key=key, day=day, percentile=percentile, half_life=half_life, lookback=lookback, method=method, mgdl=mgdl, fat_mgdl=fat_mgdl)


def place_scale(points: list[dict], place_id: int, day: date, percentile: Decimal, half_life: Decimal,
                lookback: int, method: str, mgdl: Decimal = SURCHARGE_MGDL, fat_mgdl: Decimal = Decimal(0)) -> dict:
    """How much the main meals at one place needed compared with the Automatik: the weighted median of
    actual ÷ suggested factor over the place's meals of the last PLACE_LOOKBACK_DAYS before day (each
    suggestion as the Automatik would have made it on that meal's day, with its own surcharge share).
    `scale` is None below PLACE_MIN_MEALS meals; else clamped to PLACE_SCALE_MIN..PLACE_SCALE_MAX.
    `points` must reach PLACE_LOOKBACK_DAYS + lookback days back."""
    return core.call(
        "forecast.place_scale",
        points=points, place_id=place_id, day=day, percentile=percentile, half_life=half_life, lookback=lookback, method=method, mgdl=mgdl, fat_mgdl=fat_mgdl)


def median(values: list[Decimal]) -> Decimal | None:
    """Median of sorted values; None when empty."""
    return core.call("forecast.median", values=values)


def day_type_ratios(points: list[dict], today: date, days: int = RATIO_DAYS,
                    reference_days: int = RATIO_REFERENCE_DAYS, neighbours: int = RATIO_NEIGHBOURS) -> dict:
    """Stress surcharge of work days over free days at breakfast, over the last `days` days.
    Free days are the base: work = free × (1 + surcharge). Overall: median work factor / median free
    factor − 1. Per free-day meal: the median of the same meal kind on the `neighbours` nearest work
    days before and after it (at most `reference_days` away; needs two) divided by its factor − 1, so a
    stable surcharge shows up as tightly grouped single values. For information only."""
    return core.call(
        "forecast.day_type_ratios",
        points=points, today=today, days=days, reference_days=reference_days, neighbours=neighbours)


def predict_surcharge(values: list[tuple[Decimal, int]], percentile: Decimal, half_life: Decimal,
                      target: dict, mgdl: Decimal = SURCHARGE_MGDL, fat_mgdl: Decimal = Decimal(0)) -> Decimal | None:
    """Full factor for the target meal: percentile without surcharge plus the meal's surcharge share."""
    return core.call("forecast.predict_surcharge", values=values, percentile=percentile, half_life=half_life,
                     target=target, mgdl=mgdl, fat_mgdl=fat_mgdl)


def backtest(points: list[dict], since: date, variants: list[tuple],
             target_glucose: Decimal = Decimal(120), surcharge_percentile: Decimal = Decimal("37.5"),
             surcharge_mgdl: Decimal = SURCHARGE_MGDL, fat_mgdl: Decimal = Decimal(0)) -> dict:
    """Simulate each (lookback, half-life, percentile or SURCHARGE) variant on the meals since `since`.
    Only meals every variant can predict count, so all cells compare the same meals.
    SURCHARGE uses `surcharge_percentile` on factors without `surcharge_mgdl` and adds the meal's own share.
    A variant (lookback, half-life, percentile, surcharge mg/dL) with a surcharge above 0 does the same
    at its own percentile and surcharge."""
    return core.call(
        "forecast.backtest",
        points=points, since=since, variants=variants, target_glucose=target_glucose, surcharge_percentile=surcharge_percentile, surcharge_mgdl=surcharge_mgdl, fat_mgdl=fat_mgdl)


def tuning(points: list[dict], since: date, lookback: int, half_life: Decimal, percentile: Decimal,
           surcharge_mgdl: Decimal, target_glucose: Decimal = Decimal(120),
           fat_mgdl: Decimal = Decimal(0)) -> list[dict]:
    """One row per setting: every option of that setting with the other three as configured
    (surcharge 0 = plain percentile). Each row replays the meals since `since` that all its options
    can predict, so the lookback row (which needs 8 days of history) does not shrink the others."""
    return core.call(
        "forecast.tuning",
        points=points, since=since, lookback=lookback, half_life=half_life, percentile=percentile, surcharge_mgdl=surcharge_mgdl, target_glucose=target_glucose, fat_mgdl=fat_mgdl)


def judge(options: list[dict]) -> dict:
    """Verdict of one tuning row. The best option has the most endpoints in range, then the fewest
    below. It is 'better' when it beats the configured option by more than chance; otherwise a
    spread of the lows or highs beyond chance means the setting trades lows against highs ('shift');
    else the setting makes no difference ('none')."""
    result = core.call("forecast.judge", options=options)
    for option, judged in zip(options, result["options"]):
        option["best"] = judged["best"]
    return result["verdict"]


def explain(points: list[dict], key: tuple, day: date, lookback: int, half_life: Decimal,
            percentile: Decimal, surcharge_mgdl: Decimal, fat_mgdl: Decimal = Decimal(0)) -> dict:
    """The configured suggestion for a typical meal of `key`, step by step: the meals it uses with
    their weight, the weighted percentile (of factors without surcharge), and the dose for the
    median meal size of those meals at their median KF (Σ[g × KF] / g): KE × factor + surcharge IE."""
    return core.call(
        "forecast.explain",
        points=points, key=key, day=day, lookback=lookback, half_life=half_life, percentile=percentile, surcharge_mgdl=surcharge_mgdl, fat_mgdl=fat_mgdl)


def tradeoffs(points: list[dict], since: date, lookback: int, half_life: Decimal, percentile: Decimal,
              surcharge_mgdl: Decimal, target_glucose: Decimal = Decimal(120), fat_mgdl: Decimal = Decimal(0)) -> dict:
    """Every combination of the tuning options, replayed on the same meals as the configured one.
    Lows and highs pull against each other, so there is no single best strategy: the trade-off line
    holds the combinations no other one beats on both fewer endpoints below and fewer above the range.
    Combinations with the same counts are represented by the one that changes the fewest settings."""
    return core.call(
        "forecast.tradeoffs",
        points=points, since=since, lookback=lookback, half_life=half_life, percentile=percentile, surcharge_mgdl=surcharge_mgdl, target_glucose=target_glucose, fat_mgdl=fat_mgdl)
