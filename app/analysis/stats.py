"""Order statistics on sorted Decimal values."""
from __future__ import annotations

from app import core
from decimal import Decimal


def median(values: list[Decimal]) -> Decimal:
    return core.call("stats.median", values=values)


def quartile(values: list[Decimal], fraction: Decimal) -> Decimal:
    """Linear interpolation at positions (n - 1) × fraction in sorted values."""
    return core.call("stats.quartile", values=values, fraction=fraction)


def strong_outlier_flags(values: list[Decimal]) -> list[bool]:
    """Flag unusually distant values without claiming statistical certainty."""
    return core.call("stats.strong_outlier_flags", values=values)
