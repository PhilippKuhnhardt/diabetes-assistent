"""Estimate how individual sport sessions shifted the glucose of nearby meals.

The estimate compares each sport-influenced meal with the median factor of
meals of the same type in the period that had no sport. With the model
equation IE/KE = 10 × (ΔG + Σ[IE × KF]) / Σ[g KH × KF], the glucose change
expected at the endpoint for the baseline factor F is F × Σ[g KH × KF] / 10 −
Σ[IE × KF]. The sport effect is the observed ΔG minus that expectation:
negative values mean the meal ended lower than usual, positive values higher
(for example adrenaline while racing). This is a model estimate, not a
physiological measurement or dosing advice.

The rules live in the Rust core (core/diabetes-core/src/summary.rs sport_analysis); the analysis page
calls it through summary.compute_summary.
"""
from __future__ import annotations

from decimal import Decimal

from app import core


def analyze(meals: list[dict], exercises: list[dict], lookback_minutes: int,
            window_minutes: int, labels: dict[str, str], standards: dict[str, Decimal] | None = None,
            own_sessions: set | None = None) -> dict:
    """Link sport sessions to meals and estimate each meal's sport effect."""
    return core.call("sport_analysis.analyze", meals=meals, exercises=exercises, lookback_minutes=lookback_minutes,
                     window_minutes=window_minutes, labels=labels, standards=standards,
                     own_sessions=sorted(own_sessions or ()))


def suggested_drop(standard: Decimal | None, ratios: list[Decimal]) -> str | None:
    """The kind's standard drop that would have matched what the meals measured (user decision 2026-11-27):
    the standard scaled by the median of measured ÷ assumed, rounded to 5 mg/dL, the ratio within 0..2;
    None without a standard, too few meals or no change."""
    return core.call("sport_analysis.suggested_drop", standard=standard, ratios=ratios)
