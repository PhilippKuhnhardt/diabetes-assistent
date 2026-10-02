import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import glucose


class CurrentGlucoseTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
        self.settings = {
            "timezone": "Europe/Berlin",
            "target_schedule": [
                {"start_minute": 0, "low_mgdl": "90", "high_mgdl": "140"},
                {"start_minute": 720, "low_mgdl": "100", "high_mgdl": "120"},
            ],
        }

    def status(self, value, minutes_ago=2, settings=None):
        entries = [{"at": self.now - timedelta(minutes=minutes_ago), "sgv": value}]
        return glucose.current_status(entries, settings or self.settings, self.now)

    def test_current_time_specific_target_and_color_boundaries(self):
        self.assertEqual(self.status(110)["status"], "in_range")
        for value in (70, 99, 121, 250):
            self.assertEqual(self.status(value)["status"], "outside")
        self.assertEqual(self.status(69)["status"], "low")
        self.assertEqual(self.status(251)["status"], "high")
        self.assertEqual(Decimal(self.status(110)["target_low_mgdl"]), Decimal(100))

    def test_freshness_and_missing_target_do_not_claim_in_range(self):
        self.assertEqual(self.status(110, minutes_ago=16)["status"], "stale")
        self.assertEqual(self.status(110, settings={"timezone": "Europe/Berlin",
                                                    "target_schedule": []})["status"], "no_target")
        self.assertEqual(glucose.current_status([], self.settings, self.now)["status"], "unavailable")

    def test_trend_uses_course_of_last_15_minutes(self):
        entries = [
            {"at": self.now - timedelta(minutes=12), "sgv": 100},
            {"at": self.now - timedelta(minutes=2), "sgv": 110},
        ]
        result = glucose.current_status(entries, self.settings, self.now)
        self.assertEqual(Decimal(result["trend_delta_mgdl"]), Decimal(15))
        self.assertEqual(Decimal(result["trend_minutes"]), Decimal(15))
        self.assertEqual(result["trend_direction"], "up")
        stale = glucose.current_status(entries, self.settings, self.now + timedelta(minutes=20))
        self.assertIsNone(stale["trend_delta_mgdl"])
        self.assertIsNone(stale["trend_direction"])

    def test_trend_ignores_single_outlier_and_older_values(self):
        start = self.now - timedelta(minutes=15)
        values = [180, 120, 121, 120, 119, 125]  # 180 lies 20 min back, outside the window
        entries = [{"at": start - timedelta(minutes=5), "sgv": values[0]}] + [
            {"at": start + timedelta(minutes=5 * index), "sgv": value}
            for index, value in enumerate(values[2:])]
        entries.insert(1, {"at": start - timedelta(minutes=1), "sgv": values[1]})
        result = glucose.trend(sorted((item["at"], Decimal(item["sgv"])) for item in entries))
        self.assertEqual(result["trend_direction"], "flat")
        self.assertLess(abs(Decimal(result["trend_delta_mgdl"])), Decimal(5))

    def test_trend_direction_thresholds(self):
        def direction(change):
            points = [(self.now - timedelta(minutes=15), Decimal(150)),
                      (self.now, Decimal(150) + Decimal(change))]
            return glucose.trend(points)["trend_direction"]
        self.assertEqual(direction("4.9"), "flat")
        self.assertEqual(direction("-4.9"), "flat")
        self.assertEqual(direction(5), "up")
        self.assertEqual(direction(-5), "down")
        self.assertEqual(direction(30), "up_fast")
        self.assertEqual(direction(-30), "down_fast")

    def test_trend_needs_five_minutes_of_course(self):
        points = [(self.now - timedelta(minutes=3), Decimal(100)), (self.now, Decimal(120))]
        self.assertIsNone(glucose.trend(points))
