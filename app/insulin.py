"""The rapid-insulin profile this deployment runs with (APP_INSULIN, default fiasp).

The profiles themselves (delay, peak, duration and the action curve) live only in the calculation core
(core/diabetes-core/src/insulin.rs); a fork adds one there. app/core.py passes the key to the core's context,
and the key is part of every stored day's fingerprint, so changing it recalculates the days.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from app import diabetes_core

DEFAULT = "fiasp"


@dataclass(frozen=True)
class InsulinProfile:
    key: str
    name: str


def _configured() -> InsulinProfile:
    key = os.environ.get("APP_INSULIN", DEFAULT).strip().lower() or DEFAULT
    try:
        profile = json.loads(diabetes_core.call("insulin.active", "{}", json.dumps({"insulin": key})))
    except ValueError as error:
        raise SystemExit(f"APP_INSULIN={key!r}: {error}") from None
    return InsulinProfile(profile["key"], profile["name"])


ACTIVE = _configured()
