"""Compare the Rust day (core/diabetes-core/src/day.rs) with the Python compute_day on every mirrored day of a
database. Nothing is written to it; the database stays where it is (never commit one).

    cargo build --release --manifest-path core/Cargo.toml
    python3 core/parity/days.py path/to/app.sqlite3 [--show N] [--days N]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
os.environ["APP_RUST_CORE"] = "0"
for name in ("NIGHTSCOUT_URL", "NIGHTSCOUT_CREDENTIAL"):
    os.environ.pop(name, None)

from compare import BINARY, differences  # noqa: E402


def kinds(want, got, path="$"):
    """Where a JSON type differs although the values agree (a number sent as a string, or the reverse)."""
    if isinstance(want, dict) and isinstance(got, dict):
        for key in want.keys() & got.keys():
            yield from kinds(want[key], got[key], f"{path}.{key}")
    elif isinstance(want, list) and isinstance(got, list):
        for index, (a, b) in enumerate(zip(want, got)):
            yield from kinds(a, b, f"{path}[{index}]")
    elif want is not None and got is not None and type(want) is not type(got) \
            and not (isinstance(want, (int, float)) and isinstance(got, (int, float))):
        yield path, want, got


def main():
    source = Path(sys.argv[1])
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 3
    limit = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else None
    work = Path(tempfile.mkdtemp()) / "app.sqlite3"
    shutil.copy(source, work)
    from app.store import db
    db.DB_PATH = work
    from app import i18n
    from app.services import day_view
    from app.store import timezones
    with db.connect() as conn:
        days = [row[0] for row in conn.execute("SELECT day FROM synced_days ORDER BY day")]
    days = days[-limit:] if limit else days
    context = {"language": i18n.LANGUAGE, "switches": [[at.isoformat(), zone] for at, zone in timezones.switches()]}
    expected, feed = {}, []
    for day in days:
        expected[day] = json.loads(json.dumps(day_view.compute_day(day), default=str))
        feed.append(json.dumps({"fn": "day.compute_day", "args": day_view.day_snapshot(day), "context": context}))
    answers = subprocess.run([str(BINARY)], input="\n".join(feed) + "\n", capture_output=True, text=True, check=True).stdout.splitlines()
    bad = typed = 0
    for day, line in zip(days, answers):
        answer = json.loads(line)
        if "error" in answer:
            bad += 1
            print(f"ERROR {day}: {answer['error']}")
            continue
        found = list(differences(expected[day], answer["ok"]))
        kind = list(kinds(expected[day], answer["ok"]))
        typed += bool(kind)
        if found:
            bad += 1
            print(f"DIFF {day}: {len(found)}")
            for path, a, b in found[:show]:
                print(f"       {path}: python={str(a)[:100]} rust={str(b)[:100]}")
        for path, a, b in kind[:show]:
            print(f"  type {day} {path}: python={a!r:.60} rust={b!r:.60}")
    print(f"{len(days) - bad}/{len(days)} days agree, {typed} with JSON type differences")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
