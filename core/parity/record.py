"""Record calls of the Python reference's core functions while its test suite runs.

Every distinct call (arguments as JSON) is stored with its result in calls.jsonl, so compare.py can
replay it against the Rust core. Run from the repository root:

    python3 core/parity/record.py [unittest discover args]

The recorded data is synthetic test data only; never record from a real database.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).with_name("calls.jsonl")
LIMIT = 1500  # distinct calls kept per function

from app import (calculation, correction_check, correction_scale, dosing, episodes, forecast, fpe, glucose,  # noqa: E402
                 glucose_alerts, i18n, on_board, split)
from app.analysis import stats  # noqa: E402
from app.store import timezones  # noqa: E402

TARGETS = {
    calculation: ["minutes_between", "insulin_remaining", "schedule_value_at", "isf_at", "interpolate_glucose",
                  "finger_glucose", "prebolus_start", "automatic_window", "window_totals", "calculate_episode"],
    episodes: ["normalize_entries", "normalize_treatments", "group_diary_followups", "meal_kind_at", "preceding_meal",
               "taken_meal", "meals_for_day"],
    on_board: ["stomach", "stomach_grams", "surcharge_remaining", "sport_remaining", "forecast_band", "sources",
               "effects_at", "summary", "timeline"],
    dosing: ["half_units", "suggest"],
    split: ["offer", "worth_it", "parts", "open_plan", "timing"],
    correction_scale: ["meal_rise", "scale", "dose_range"],
    glucose: ["limits", "shown", "target_at", "tone", "target_bands", "trend", "current_status"],
    fpe: ["units", "minutes", "estimate"],
    forecast: ["weighted_quantile", "weight", "surcharge_share", "history", "predict", "predict_method", "place_scale",
               "median", "day_type_ratios", "backtest", "tuning", "tradeoffs", "explain"],
    glucose_alerts: ["forecast", "warnings"],
    correction_check: ["due"],
    stats: ["median", "quartile", "strong_outlier_flags"],
}

seen: dict[str, set] = {}
handle = None


def encode(value):
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {"|".join(map(str, key)) if isinstance(key, tuple) else str(key): encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(item) for item in value]
    if callable(value):
        return None
    return str(value)


def special(name: str, args: dict) -> dict | None:
    """Callables as data; None skips the call (an input the Rust interface does not take)."""
    if name == "on_board.sources":
        if args.get("surcharge_share") is not forecast.surcharge_share and args.get("surcharge_mgdl") is not None:
            return None
        factor, scale = args["carb_factor"], args.get("scale")
        args = dict(args)
        args["carb_factors"] = [[item["at"], factor(item["at"])] for item in args["treatments"]
                                if calculation.dec(item.get("carbs") or 0) > 0]
        args["scales"] = [scale(item) for item in args["treatments"]] if scale else None
    if name == "dosing.suggest":
        share = args.get("surcharge_share")
        args = dict(args)
        grams = calculation.dec(args.get("carbs") or 0)
        args["surcharge_share"] = share(grams) if callable(share) and grams.is_finite() and grams > 0 else None
    if name == "on_board.timeline":
        args = dict(args)
        args["horizon_seconds"] = args.pop("horizon")
    return args


def wrap(module, function):
    name = f"{module.__name__.removeprefix('app.').removeprefix('analysis.')}.{function.__name__}"
    signature = inspect.signature(function)

    @functools.wraps(function)
    def recorded(*args, **kwargs):
        try:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            prepared = special(name, dict(bound.arguments))
            text = json.dumps(encode(prepared), sort_keys=True) if prepared is not None else None
        except Exception:  # noqa: BLE001 - recording must never change the test run
            text = None
        try:
            result = function(*args, **kwargs)
        except (ValueError, ArithmeticError) as error:
            outcome = {"error": str(error), "kind": "arithmetic" if isinstance(error, ArithmeticError) else "value"}
            store(name, text, outcome)
            raise
        if name == "episodes.group_diary_followups":
            store(name, text, {"ok": encode(bound.arguments["treatments"])})
        else:
            store(name, text, {"ok": encode(result)})
        return result
    return recorded


def store(name, text, outcome):
    if text is None:
        return
    digest = hashlib.sha1(text.encode()).hexdigest()
    keep = seen.setdefault(name, set())
    if digest in keep or len(keep) >= LIMIT:
        return
    keep.add(digest)
    context = {"language": i18n.LANGUAGE,
               "switches": [[at.isoformat(), zone] for at, zone in timezones.switches()]}
    handle.write(json.dumps({"fn": name, "args": json.loads(text), "context": context, "expect": outcome}) + "\n")


def wrap_dose_help():
    """Record services.dose_help with the snapshot it reads from the store (services.rs Snapshot)."""
    from app.services import day_view, dose_help as module, factors
    from app.store import config, marks
    from app.store import places as place_store
    original = module.dose_help

    def recorded(payload):
        now = module.datetime.now(timezone.utc)
        captured = {}

        class Frozen(module.datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        def capturing(function, key):
            def inner(*args, **kwargs):
                captured[key] = function(*args, **kwargs)
                return captured[key]
            return inner

        patches = [(module, "datetime", Frozen)] + [
            (owner, name, capturing(getattr(owner, name), key))
            for owner, name, key in ((day_view, "recent", "recent"), (marks, "sport_session_effects", "sport"),
                                     (marks, "sport_session_minutes", "sport_minutes"),
                                     (place_store, "entry_places", "entry_places"))]
        saved = [(owner, name, getattr(owner, name)) for owner, name, _ in patches]
        for owner, name, value in patches:
            setattr(owner, name, value)
        try:
            result = original(payload)
            outcome = {"ok": encode(result)}
        except (ValueError, ArithmeticError) as error:
            outcome = {"error": str(error), "kind": "value"}
            raise
        finally:
            for owner, name, value in saved:
                setattr(owner, name, value)
            record_dose_help(now, payload, captured, outcome)
        return result

    def mocked():
        """A test replaced a function the dose help reads; its answer then does not follow the snapshot."""
        from unittest import mock
        from app import fpe as fpe_module
        owners = ((forecast, "predict_method"), (forecast, "place_scale"), (factors, "meal_points"),
                  (factors, "meal_factor_values"), (factors, "place_scales"), (factors, "carb_factor"),
                  (dosing, "suggest"), (on_board, "sources"), (on_board, "summary"), (split, "open_plan"),
                  (fpe_module, "estimate"), (marks, "day_type_for"), (marks, "analysis_day_types"),
                  (marks, "vacation_periods"), (place_store, "all_places"), (config, "get_settings"))
        return any(isinstance(getattr(owner, name), mock.NonCallableMock) for owner, name in owners)

    def record_dose_help(now, payload, captured, outcome):
        if "recent" not in captured or mocked():
            return
        try:
            settings = config.get_settings()[1]
            today = now.astimezone(module.local_zone(settings["timezone"])).date()
            overrides, periods = marks.analysis_day_types(), marks.vacation_periods()
            back = max(int(settings["auto_lookback_days"]) + 60, 28) + 5
            points = factors.meal_points(today, back, overrides, periods, mode=factors.vacation_mode(settings))
            snapshot = {
                "now": now, "settings": settings, "payload": payload,
                "entries": captured["recent"]["entries"], "treatments": captured["recent"]["treatments"],
                "day_types": overrides, "vacation_periods": periods, "meal_points": points,
                "places": place_store.all_places(), "entry_places": captured.get("entry_places", {}),
                "sport_overrides": captured.get("sport", {}), "sport_minutes_overrides": captured.get("sport_minutes", {}),
                "auto_vacation": factors.vacation_mode(settings),
            }
            store("services.dose_help", json.dumps(encode(snapshot), sort_keys=True), outcome)
        except Exception as error:  # noqa: BLE001 - recording must never change the test run
            print("dose_help not recorded:", repr(error), file=sys.stderr)

    module.dose_help = recorded


def main():
    global handle
    for module, names in TARGETS.items():
        for function_name in names:
            setattr(module, function_name, wrap(module, getattr(module, function_name)))
    wrap_dose_help()
    handle = OUT.open("w")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), *sys.argv[1:2])
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    handle.close()
    print({name: len(found) for name, found in sorted(seen.items())})
    print("tests ok" if result.wasSuccessful() else f"tests failed: {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    main()
