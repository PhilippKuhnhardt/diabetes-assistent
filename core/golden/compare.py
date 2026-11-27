"""Replay the golden calls (calls.jsonl.gz) against the Rust core and compare.

The calls were recorded from the former Python reference while its test suite ran (2026-11-27, synthetic
test data only), before the rules moved to Rust: every core function with its arguments and the result
Python computed. They pin the rules: a change that moves a result shows up here and is either a bug or a
deliberate rule change, which then needs new expectations (edit the "expect" of the affected lines).

Numbers agree when they differ by at most 1e-9 relative (rust_decimal and Python's Decimal round the
last of their ~28 digits differently, and the stomach model runs on floats); times by instant; texts
exactly. A field the Rust result leaves out counts only when the expected value is empty.

    cargo build --release --manifest-path core/Cargo.toml
    python3 core/golden/compare.py [--show N]
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
BINARY = HERE.parent / "target" / "release" / "core-call"
TOLERANCE = Decimal("1e-9")


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def instant(value):
    if not isinstance(value, str) or len(value) < 16 or value[4] != "-":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def empty(value):
    return value is None or value == [] or value == {} or value is False


def differences(want, got, path="$"):
    """Yield (path, python, rust) for every disagreement."""
    if isinstance(want, dict) and isinstance(got, dict):
        for key in want:
            if key not in got:
                if not empty(want[key]):
                    yield f"{path}.{key}", want[key], "<missing>"
                continue
            yield from differences(want[key], got[key], f"{path}.{key}")
        return
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            yield f"{path}[len]", len(want), len(got)
            return
        for index, (a, b) in enumerate(zip(want, got)):
            yield from differences(a, b, f"{path}[{index}]")
        return
    if want == got:
        return
    a, b = instant(want), instant(got)
    if a and b:
        if a != b:
            yield path, want, got
        return
    a, b = number(want), number(got)
    if a is not None and b is not None:
        if abs(a - b) > TOLERANCE * max(Decimal(1), abs(a)):
            yield path, want, got
        return
    if empty(want) and empty(got):
        return
    yield path, want, got


def main():
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 3
    calls = [json.loads(line) for line in gzip.open(HERE / "calls.jsonl.gz", "rt", encoding="utf-8")]
    feed = "".join(json.dumps({"fn": c["fn"], "args": c["args"], "context": c["context"]}) + "\n" for c in calls)
    answers = subprocess.run([str(BINARY)], input=feed, capture_output=True, text=True, check=True).stdout.splitlines()
    totals, failures = defaultdict(int), defaultdict(list)
    for call, line in zip(calls, answers):
        answer, expect = json.loads(line), call["expect"]
        totals[call["fn"]] += 1
        if "error" in expect or "error" in answer:
            if expect.get("error") != answer.get("error"):
                failures[call["fn"]].append([("$error", expect.get("error"), answer.get("error") or "<ok>")])
            continue
        found = list(differences(expect["ok"], answer["ok"]))
        if found:
            failures[call["fn"]].append(found)
    bad = 0
    for name in sorted(totals):
        failed = failures.get(name, [])
        bad += len(failed)
        print(f"{'ok  ' if not failed else 'DIFF'} {name}: {totals[name] - len(failed)}/{totals[name]}")
        for found in failed[:show]:
            for path, a, b in found[:4]:
                print(f"       {path}: expected={str(a)[:120]} rust={str(b)[:120]}")
    print(f"{sum(totals.values()) - bad}/{sum(totals.values())} calls agree")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
