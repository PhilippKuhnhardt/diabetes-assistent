"""The background round reads and computes each thing once (CPU load of the minute-by-minute sync)."""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.common import local_day_range
from app.services import day_view, summary
from app.store import db, mirror, results

TODAY, YESTERDAY = "2026-10-30", "2026-10-29"


def reading(at: datetime, sgv: int) -> dict:
    return {"_id": f"e{int(at.timestamp())}", "date": int(at.timestamp() * 1000), "type": "sgv", "sgv": sgv,
            "dateString": at.isoformat().replace("+00:00", "Z")}


class RoundLoadTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def mirror(self, day, entries):
        start, end = local_day_range(day, "Europe/Berlin")
        mirror.mirror_day(day, [entry for entry in entries if start.timestamp() * 1000 <= entry["date"] < end.timestamp() * 1000],
                          [], start, end)

    def test_new_reading_today_leaves_yesterday_unchanged(self):
        # Yesterday's range reaches into today until 13:00, so both days mirror the readings in between.
        shared = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
        entries = [reading(shared + timedelta(minutes=minute), 100 + minute) for minute in range(0, 600, 5)]
        self.mirror(YESTERDAY, entries)
        self.mirror(TODAY, entries)
        before = day_view.day_fingerprint(YESTERDAY), day_view.day_fingerprint(TODAY)
        entries.append(reading(shared + timedelta(minutes=601), 150))
        self.mirror(TODAY, entries)
        self.mirror(YESTERDAY, entries)
        self.assertEqual(day_view.day_fingerprint(YESTERDAY), before[0])
        self.assertNotEqual(day_view.day_fingerprint(TODAY), before[1])
        # A changed reading in the shared hours still changes both days.
        entries[0] = {**entries[0], "sgv": 99}
        self.mirror(TODAY, entries)
        self.assertNotEqual(day_view.day_fingerprint(YESTERDAY), before[0])
        # Rows keep Nightscout's order, also when only some of them are rewritten.
        start, end = local_day_range(TODAY, "Europe/Berlin")
        self.assertEqual([row["sgv"] for row in mirror.cached_between(start, end)[0]],
                         [entry["sgv"] for entry in entries])

    def test_one_pass_reads_each_day_once_and_afresh_after_a_sync(self):
        real = results.day_inputs
        with patch("app.store.results.day_inputs", side_effect=real) as inputs:
            with day_view.one_pass():
                first = day_view.day_payload(TODAY, refresh=False)
                again = day_view.day_payload(TODAY, refresh=False)
                day_view.day_fingerprint(TODAY)
                self.assertEqual(inputs.call_count, 1)
                self.assertEqual(first, again)
                self.assertIsNot(first, again)
                with patch("app.ingestion.sync.sync_day", return_value={}):
                    day_view.day_payload(TODAY)
                self.assertEqual(inputs.call_count, 2)
            day_view.day_fingerprint(TODAY)
            day_view.day_fingerprint(TODAY)
            self.assertEqual(inputs.call_count, 4)

    def test_automatik_part_follows_its_meals(self):
        # The core keeps the Automatik part while its meals and settings are unchanged (summary.rs AUTOMATIK_CACHE):
        # another period gives the same part, a new meal a new one.
        points = []
        with patch("app.services.day_view.day_payload", return_value={"meals": []}), \
                patch("app.services.factors.meal_points", side_effect=lambda *args, **kwargs: list(points)):
            first = summary.compute_summary(14, date(2026, 10, 30))
            self.assertEqual(summary.compute_summary(30, date(2026, 10, 30))["suggestion_derivation"],
                             first["suggestion_derivation"])
            points.extend({"id": f"m{age}", "day": date(2026, 10, 30 - age),
                           "at": datetime(2026, 10, 30 - age, 6, tzinfo=timezone.utc), "key": ("breakfast", None),
                           "day_type": None, "value": summary.Decimal(1), "end_glucose": None,
                           "carbs_isf": summary.Decimal(4000), "carbs_g": summary.Decimal(40)} for age in range(1, 6))
            changed = summary.compute_summary(14, date(2026, 10, 30))["suggestion_derivation"]
            self.assertNotEqual(changed, first["suggestion_derivation"])


if __name__ == "__main__":
    unittest.main()
