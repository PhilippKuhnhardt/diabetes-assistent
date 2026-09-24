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

    def test_trend_uses_previous_measurement(self):
        entries = [
            {"at": self.now - timedelta(minutes=12), "sgv": 100},
            {"at": self.now - timedelta(minutes=2), "sgv": 110},
        ]
        result = glucose.current_status(entries, self.settings, self.now)
        self.assertEqual(Decimal(result["trend_delta_mgdl"]), Decimal(10))
        self.assertEqual(Decimal(result["trend_minutes"]), Decimal(10))
        stale = glucose.current_status(entries, self.settings, self.now + timedelta(minutes=20))
        self.assertIsNone(stale["trend_delta_mgdl"])
