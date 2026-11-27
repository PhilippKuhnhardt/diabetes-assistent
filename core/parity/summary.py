"""Compare the Rust analysis page (core/diabetes-core/src/summary.rs) with the Python compute_summary for every
period on a copy of a database. Nothing is written to it; the database stays where it is (never commit one).

    cargo build --release --manifest-path core/Cargo.toml
    python3 core/parity/summary.py path/to/app.sqlite3 [period ...]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
os.environ["APP_RUST_CORE"] = "0"
for name in ("NIGHTSCOUT_URL", "NIGHTSCOUT_CREDENTIAL"):
    os.environ.pop(name, None)

from compare import BINARY, differences  # noqa: E402
from days import kinds  # noqa: E402


def main():
    work = Path(tempfile.mkdtemp()) / "app.sqlite3"
    shutil.copy(sys.argv[1], work)
    from app.store import db
    db.DB_PATH = work
    from app import i18n
    from app.common import json_safe
    from app.services import summary
    from app.store import timezones
    with db.connect() as conn:
        today = date.fromisoformat(conn.execute("SELECT max(day) FROM synced_days").fetchone()[0])
    periods = [int(value) for value in sys.argv[2:]] or [1, 3, 7, 14, 30]
    context = {"language": i18n.LANGUAGE, "switches": [[at.isoformat(), zone] for at, zone in timezones.switches()]}
    expected, feed = {}, []
    for days in periods:
        expected[days] = json.loads(json.dumps(json_safe(summary.compute_summary(days, today)), default=str))
        feed.append(json.dumps({"fn": "summary.compute_summary", "args": summary.summary_snapshot(days, today),
                                "context": context}, default=str))
    answers = subprocess.run([str(BINARY)], input="\n".join(feed) + "\n", capture_output=True, text=True, check=True).stdout.splitlines()
    bad = 0
    for days, line in zip(periods, answers):
        answer = json.loads(line)
        if "error" in answer:
            bad += 1
            print(f"ERROR {days} days: {answer['error']}")
            continue
        found, kind = list(differences(expected[days], answer["ok"])), list(kinds(expected[days], answer["ok"]))
        bad += bool(found)
        print(f"{days} days: {len(found)} differences, {len(kind)} JSON type differences")
        for path, a, b in (found + kind)[:10]:
            print(f"       {path}: python={str(a)[:100]} rust={str(b)[:100]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
