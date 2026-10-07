"""Compare the app's calculated tables on real data before and after a deployment.

    python3 scripts/compare_api.py snapshot https://deine-domain before.json
    # deploy
    python3 scripts/compare_api.py snapshot https://deine-domain after.json
    python3 scripts/compare_api.py compare before.json after.json

A snapshot reads /api/v1/data from the first stored day up to yesterday (today still
changes), so a refactoring that keeps the math must report no difference. The bearer key
comes from DIABETES_API_KEY when set; snapshots hold personal data and stay out of the repo.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date, timedelta


def fetch(base: str, path: str) -> dict:
    request = urllib.request.Request(base.rstrip("/") + path)
    key = os.environ.get("DIABETES_API_KEY")
    if key:
        request.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def snapshot(base: str, target: str) -> None:
    index = fetch(base, "/api/v1")
    last = (date.fromisoformat(index["today"]) - timedelta(days=1)).isoformat()
    first = index["first_day"] or last
    data = fetch(base, f"/api/v1/data?from={first}&to={last}")
    data["profile"] = fetch(base, "/api/v1/profile")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True)
    print(f"{target}: {first} bis {last}, " +
          ", ".join(f"{name} {len(rows)}" for name, rows in data["tables"].items()))


def compare(before_path: str, after_path: str) -> int:
    with open(before_path, encoding="utf-8") as handle:
        before = json.load(handle)
    with open(after_path, encoding="utf-8") as handle:
        after = json.load(handle)
    differences = 0
    if before["profile"] != after["profile"]:
        print("Einstellungen oder Profil unterscheiden sich")
        differences += 1
    for name in sorted(set(before["tables"]) | set(after["tables"])):
        old = before["tables"].get(name, [])
        new = after["tables"].get(name, [])
        if old == new:
            print(f"{name}: gleich ({len(old)} Zeilen)")
            continue
        differences += 1
        print(f"{name}: {len(old)} → {len(new)} Zeilen")
        for row_number, (left, right) in enumerate(zip(old, new)):
            changed = {key for key in set(left) | set(right) if left.get(key) != right.get(key)}
            if changed:
                print(f"  Zeile {row_number}: " + "; ".join(
                    f"{key} {left.get(key)!r} → {right.get(key)!r}" for key in sorted(changed)))
                break
    return 1 if differences else 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "snapshot":
        snapshot(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(2)
