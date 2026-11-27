"""Time the heaviest recorded Automatik calls (backtest, tuning, trade-offs) in Python and in Rust.

    python3 core/parity/bench.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from app import forecast  # noqa: E402

NAMES = ("tradeoffs", "tuning", "backtest")


def point(raw: dict) -> dict:
    dec = lambda value: Decimal(value) if value is not None else None
    return {**raw, "day": date.fromisoformat(raw["day"]),
            "real_day": date.fromisoformat(raw["real_day"]) if raw.get("real_day") else None,
            "at": datetime.fromisoformat(raw["at"]), "key": tuple(raw["key"]), "value": Decimal(raw["value"]),
            "end_glucose": dec(raw.get("end_glucose")), "carbs_isf": dec(raw.get("carbs_isf")),
            "carbs_g": dec(raw.get("carbs_g")), "surcharge_mgdl": dec(raw.get("surcharge_mgdl"))}


def python_args(name: str, args: dict) -> dict:
    out = dict(args)
    out["points"] = [point(item) for item in args["points"]]
    out["since"] = date.fromisoformat(args["since"])
    for key in ("half_life", "percentile", "surcharge_mgdl", "target_glucose", "fat_mgdl", "surcharge_percentile"):
        if out.get(key) is not None:
            out[key] = Decimal(out[key])
    if name == "backtest":
        out["variants"] = [tuple(v if isinstance(v, (int, str)) and (i == 0 or v == forecast.SURCHARGE) else Decimal(str(v))
                                 for i, v in enumerate(variant)) for variant in args["variants"]]
    return out


def main():
    calls = {}
    for line in (HERE / "calls.jsonl").open():
        call = json.loads(line)
        name = call["fn"].split(".")[-1]
        if call["fn"].startswith("forecast.") and name in NAMES:
            best = calls.get(name)
            if best is None or len(call["args"]["points"]) > len(best["args"]["points"]):
                calls[name] = call
    for name in NAMES:
        call = calls.get(name)
        if not call:
            continue
        args = python_args(name, call["args"])
        started = time.perf_counter()
        getattr(forecast, name)(**args)
        python_seconds = time.perf_counter() - started
        feed = json.dumps({"fn": call["fn"], "args": call["args"]}) + "\n"
        started = time.perf_counter()
        subprocess.run([str(HERE.parent / "target" / "release" / "core-call")], input=feed, capture_output=True, text=True, check=True)
        rust_seconds = time.perf_counter() - started
        print(f"{name}: {len(call['args']['points'])} meals, Python {python_seconds:.3f} s, Rust {rust_seconds:.3f} s (incl. process start and JSON)")


if __name__ == "__main__":
    main()
