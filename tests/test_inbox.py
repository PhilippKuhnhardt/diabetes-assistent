import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.services import inbox
from app.store import db

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
ZONE = ZoneInfo("Europe/Berlin")


def meal(meal_id, open_items, end="2026-10-21T14:00:00+00:00", excluded=False, at="2026-10-21T10:30:00+00:00"):
    return {"id": meal_id, "meal": "lunch", "at": at, "excluded": excluded,
            "review": {"open": open_items}, "window": {"end": end},
            "calculation": {"reason": "CGM-Lücke am Messzeitpunkt ist zu groß"}}


class MealItemTests(unittest.TestCase):
    """The start page lists finished meals that still need an action (user request 2026-10-21)."""

    def test_only_finished_open_meals_that_count(self):
        day = {"day": "2026-10-21", "meals": [
            meal("open", ["blocked"]),
            meal("clean", []),
            meal("running", ["blocked"], end="2026-10-21T19:00:00+00:00"),
            meal("excluded", ["blocked"], excluded=True),
        ]}
        items = inbox.meal_items(day, NOW, ZONE)
        self.assertEqual([item["meal_id"] for item in items], ["open"])
        self.assertEqual(items[0]["key"], "meal:open:blocked")
        self.assertEqual(items[0]["reasons"], [{"kind": "blocked", "text": "Faktor nicht berechenbar: CGM-Lücke am Messzeitpunkt ist zu groß"}])


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_opened_hint_loses_its_highlight_and_vanishes_once_done(self):
        days = {"2026-10-21": {"day": "2026-10-21", "meals": [meal("m", ["blocked"])]},
                "2026-10-20": {"day": "2026-10-20", "meals": [
                    meal("y", ["blocked"], end="2026-10-20T14:00:00+00:00", at="2026-10-20T10:30:00+00:00")]}}
        payload = lambda day, refresh: days.get(day, {"day": day, "meals": []})
        with patch.object(inbox.day_view, "day_payload", side_effect=payload):
            first = inbox.view(now=NOW)
            self.assertEqual([item["meal_id"] for item in first["items"]], ["m", "y"])
            self.assertEqual(first["new"], 2)
            inbox.store.mark_seen("meal:m:blocked")
            second = inbox.view(now=NOW)
            self.assertEqual([item["seen"] for item in second["items"]], [True, False])
            self.assertEqual(second["new"], 1)
            days["2026-10-21"]["meals"][0]["review"]["open"] = []
            self.assertEqual([item["meal_id"] for item in inbox.view(now=NOW)["items"]], ["y"])

    def test_mark_seen_needs_a_key(self):
        with self.assertRaises(ValueError):
            inbox.mark_seen({})


if __name__ == "__main__":
    unittest.main()
