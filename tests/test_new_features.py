import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app import auth, dosing, episodes, nightscout


class DosingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
        self.settings = {
            "timezone": "Europe/Berlin",
            "ic_schedule": [{"start_minute": 0, "grams_per_unit": "7.142857142857142857142857143"}],
            "isf_schedule": [{"start_minute": 0, "mgdl_per_unit": "40"}],
            "target_schedule": [{"start_minute": 0, "low_mgdl": "100", "high_mgdl": "120"}],
        }

    def test_ie_per_ke_and_correction_use_current_target_midpoint(self):
        entries = [
            {"at": self.now - timedelta(minutes=15), "sgv": 130},
            {"at": self.now - timedelta(minutes=5), "sgv": 150},
        ]
        result = dosing.suggest("20", self.settings, entries, [], self.now)
        self.assertAlmostEqual(float(result["units_per_ke"]), 1.4)
        self.assertAlmostEqual(float(result["carb_units"]), 2.8)
        self.assertEqual(Decimal(result["correction_units"]), Decimal(1))
        self.assertAlmostEqual(float(result["total_units"]), 3.8)
        self.assertEqual(Decimal(result["trend_delta_mgdl"]), Decimal(20))

    def test_stale_glucose_has_no_correction(self):
        entries = [{"at": self.now - timedelta(minutes=16), "sgv": 150}]
        result = dosing.suggest("20", self.settings, entries, [], self.now)
        self.assertIn("carb_units", result)
        self.assertNotIn("total_units", result)
        self.assertFalse(result["glucose_fresh"])

    def test_missing_target_still_shows_glucose(self):
        settings = {**self.settings, "target_schedule": []}
        result = dosing.suggest("10", settings,
                                [{"at": self.now, "sgv": 135}], [], self.now)
        self.assertEqual(result["glucose_mgdl"], "135")
        self.assertIn("carb_units", result)
        self.assertNotIn("total_units", result)


class ProfileTargetTests(unittest.TestCase):
    def test_nightscout_targets_are_normalized_to_mgdl(self):
        profile = {
            "_id": "p1", "startDate": "2026-09-01T00:00:00Z",
            "defaultProfile": "Default",
            "store": {"Default": {
                "timezone": "Europe/Berlin", "units": "mmol/L",
                "sens": [{"time": "00:00", "value": 2.5}],
                "carbratio": [{"time": "00:00", "value": 10}],
                "target_low": [{"time": "00:00", "value": 5}],
                "target_high": [{"time": "00:00", "value": 6}],
            }},
        }
        with patch.object(nightscout, "fetch_current_profile", return_value=profile):
            target = nightscout.fetch_profiles()[0]["target_schedule"][0]
        self.assertEqual(Decimal(target["low_mgdl"]), Decimal("90.0910"))
        self.assertEqual(Decimal(target["high_mgdl"]), Decimal("108.1092"))


class MealClassificationTests(unittest.TestCase):
    def test_configurable_cutoffs_classify_unlinked_nightscout_meals(self):
        at = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)  # 11:30 in Berlin
        treatments = [{"id": "meal-1", "at": at, "eventType": "Meal Bolus",
                       "carbs": 30, "insulin": 0, "notes": ""}]
        settings = {"breakfast_end_minute": 720, "lunch_end_minute": 1080}
        meals = episodes.meals_for_day(treatments, "2026-09-24", "Europe/Berlin", settings)
        self.assertEqual(meals[0]["meal"], "breakfast")
        settings["breakfast_end_minute"] = 660
        meals = episodes.meals_for_day(treatments, "2026-09-24", "Europe/Berlin", settings)
        self.assertEqual(meals[0]["meal"], "lunch")


class AuthTests(unittest.TestCase):
    def test_browser_cookie_survives_calls_and_changes_with_password(self):
        with patch.dict(os.environ, {"APP_PASSWORD": "private-pass"}):
            cookie = auth.cookie_header()
            self.assertTrue(auth.authorized(cookie))
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Max-Age=", cookie)
            self.assertFalse(auth.authorized(""))
            with patch.dict(os.environ, {"APP_PASSWORD": "new-pass"}):
                self.assertFalse(auth.authorized(cookie))


if __name__ == "__main__":
    unittest.main()
