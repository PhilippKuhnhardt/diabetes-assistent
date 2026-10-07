"""Order statistics on sorted Decimal values."""
from __future__ import annotations

from decimal import Decimal



def median(values: list[Decimal]) -> Decimal:
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def quartile(values: list[Decimal], fraction: Decimal) -> Decimal:
    """Linear interpolation at positions (n - 1) × fraction in sorted values."""
    position = Decimal(len(values) - 1) * fraction
    lower = int(position)
    share = position - lower
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * share


def strong_outlier_flags(values: list[Decimal]) -> list[bool]:
    """Flag unusually distant values without claiming statistical certainty."""
    if len(values) < 3:
        return [False] * len(values)
    center = median(sorted(values))
    typical_distance = median(sorted(abs(value - center) for value in values))
    threshold = max(3 * typical_distance, center * Decimal("0.5"))
    return [abs(value - center) > threshold for value in values]
