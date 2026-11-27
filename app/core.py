"""The one bridge to the calculation core in Rust (core/, imported as app.diabetes_core).

Every calculation rule lives in Rust; the Python modules calculation, episodes, on_board, dosing, split,
correction_scale, fpe, glucose, glucose_alerts, correction_check, forecast and analysis.stats only keep
their names and signatures and call `call("module.function", **args)` here. Arguments go out as JSON
(Decimal and datetime as text) and results come back with the Python types the callers expect:
core_types.json lists per function and JSON path which values are Decimal, datetime, date or tuple
(recorded once from the former Python reference; add an entry by hand for a new core function).

Build the extension with `python3 core/build.py` (it copies the library to app/diabetes_core.abi3.so).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app import diabetes_core

TYPES: dict[str, dict[str, list[str]]] = json.loads((Path(__file__).with_name("core_types.json")).read_text())
_INSTANT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)$")
_DAY = re.compile(r"^\d{4}-\d\d-\d\d$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")
_context: tuple | None = None


def encode(value):
    """Arguments as JSON values: Decimal and datetimes as text, tuples and sets as lists."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {"|".join(map(str, key)) if isinstance(key, tuple) else str(key): encode(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(item) for item in value]
    if callable(value):
        return None
    return str(value)


def _number(text: str, kinds: list[str]):
    if "Decimal" in kinds:
        if "int" in kinds and "." not in text and "e" not in text.lower():
            return int(text)
        return Decimal(text)
    if "int" in kinds and "float" in kinds:
        return int(text) if "." not in text else float(text)
    if "int" in kinds:
        return int(Decimal(text))
    if "float" in kinds:
        return float(text)
    return text


def _decode(value, path: str, types: dict[str, list[str]]):
    kinds = types.get(path)
    if isinstance(value, dict):
        return {key: _decode(item, f"{path}.{key}", types) for key, item in value.items()}
    if isinstance(value, list):
        items = [_decode(item, path + "[]", types) for item in value]
        return tuple(items) if kinds and "tuple" in kinds else items
    if isinstance(value, str) and _INSTANT.match(value) and not (kinds and "str" in kinds):
        # Instants: a datetime where the callers expect one, else the text as Python writes it.
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment if kinds and "datetime" in kinds else moment.isoformat()
    if kinds is None or value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if "date" in kinds and _DAY.match(value):
            return date.fromisoformat(value)
        if "str" not in kinds and _NUMBER.match(value):
            return _number(value, kinds)
        return value
    if isinstance(value, (int, float)):
        if "Decimal" in kinds and not (isinstance(value, int) and "int" in kinds):
            return Decimal(str(value))
        if "float" in kinds and isinstance(value, int) and "int" not in kinds:
            return float(value)
    return value


def _configure() -> None:
    global _context
    from app import i18n
    from app.store import timezones
    context = (i18n.LANGUAGE, timezones.switches())
    if context != _context:
        diabetes_core.configure(json.dumps({"language": context[0],
                                            "switches": [[at.isoformat(), zone] for at, zone in context[1]]}))
        _context = context


def call(name: str, **args):
    """Run `module.function` in the Rust core with the reference's parameter names."""
    _configure()
    result = json.loads(diabetes_core.call(name, json.dumps(encode(args))))
    return _decode(result, "$", TYPES.get(name, {}))


def now_or(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def update_in_place(items: list[dict], updated: list[dict]) -> None:
    """Functions that change their input in place: write the core's result back into the same dicts."""
    for item, new in zip(items, updated):
        item.update(new)


def share_value(surcharge_share, carbs):
    """dosing.suggest takes the share the entry's carbs add (a callable in the reference) as a value."""
    if not callable(surcharge_share):
        return surcharge_share
    try:
        grams = Decimal(str(carbs or 0))
    except ArithmeticError:
        return None
    return surcharge_share(grams) if grams.is_finite() and grams > 0 else None


def sources(*, treatments, now, settings, carb_factor, surcharge_mgdl, keep_all, sport_overrides,
            fat_surcharge_mgdl, scale, sport_minutes_overrides) -> dict:
    """on_board.sources with its callables as data: the IE/KE per carb entry time and the place factor per
    treatment."""
    carbs = []
    for item in treatments:
        try:
            grams = Decimal(str(item.get("carbs") or 0))
        except ArithmeticError:
            continue
        if grams > 0:
            carbs.append([item["at"], carb_factor(item["at"])])
    return call("on_board.sources", treatments=treatments, now=now, settings=settings, carb_factors=carbs,
                scales=[scale(item) for item in treatments] if scale else None, surcharge_mgdl=surcharge_mgdl,
                keep_all=keep_all, sport_overrides=sport_overrides, fat_surcharge_mgdl=fat_surcharge_mgdl,
                sport_minutes_overrides=sport_minutes_overrides)
