"""Rapid-insulin action profiles: how much of a dose has not acted yet, minutes after the injection.

A profile is an exponential activity curve as used by Loop and oref (delay, peak, duration). The app is
built and tested with Fiasp; its curve fits the author's CGM data (about 0 % acted after 30 min, 50 % after
90 min, 75 % after 2 h, nearly all after 4 h). Other insulins are not part of this project, but a fork can
add one: put another InsulinProfile into PROFILES and pick it with APP_INSULIN (fixed for the process, like
APP_LANGUAGE). Every calculation (meal factors, "Wirkt noch", dose help, warnings) reads the active profile,
and stored days are recalculated when it changes (app/services/day_view.py fingerprint).
Values from Loop's presets; check them against your own data before you rely on them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class InsulinProfile:
    key: str
    name: str
    delay_minutes: Decimal  # before the curve starts
    peak_minutes: Decimal  # peak activity, counted from the end of the delay
    duration_minutes: Decimal  # total action time, counted from the end of the delay

    def remaining(self, minutes: Decimal) -> Decimal:
        """Share of a dose that has not acted yet, `minutes` after the injection."""
        t = Decimal(str(minutes)) - self.delay_minutes
        if t <= 0:
            return Decimal(1)
        peak, duration = self.peak_minutes, self.duration_minutes
        if t >= duration:
            return Decimal(0)
        tau = peak * (1 - peak / duration) / (1 - 2 * peak / duration)
        a = 2 * tau / duration
        scale = 1 / (1 - a + (1 + a) * (-duration / tau).exp())
        used = scale * (1 - a) * ((t * t / (tau * duration * (1 - a)) - t / tau - 1) * (-t / tau).exp() + 1)
        return min(Decimal(1), max(Decimal(0), 1 - used))

    @property
    def total_minutes(self) -> Decimal:
        """From the injection until nothing is left."""
        return self.delay_minutes + self.duration_minutes


PROFILES = {
    # Loop's Fiasp preset: peak 55 min after a 10-min delay, 6 h.
    "fiasp": InsulinProfile("fiasp", "Fiasp", Decimal(10), Decimal(55), Decimal(360)),
}
DEFAULT = "fiasp"


def _configured() -> InsulinProfile:
    key = os.environ.get("APP_INSULIN", DEFAULT).strip().lower() or DEFAULT
    if key not in PROFILES:
        raise SystemExit(f"APP_INSULIN={key!r} is unknown; known: {', '.join(sorted(PROFILES))}")
    return PROFILES[key]


ACTIVE = _configured()
