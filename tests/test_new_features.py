import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app import auth, dosing, episodes, nightscout, server


class DosingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
        self.settings = {
            "timezone": "Europe/Berlin",
            "ic_schedule": [{"start_minute": 0, "grams_per_unit": "7.142857142857142857142857143"}],
            "isf_schedule": [{"start_minute": 0, "mgdl_per_unit": "40"}],
            "target_schedule": [{"start_minute": 0, "low_mgdl": "100", "high_mgdl": "120"}],
        }

    def test_normal_and_cautious_targets(self):
        entries = [
            {"at": self.now - timedelta(minutes=15), "sgv": 130},
            {"at": self.now - timedelta(minutes=5), "sgv": 150},
        ]
        result = dosing.suggest("20", self.settings, entries, [], self.now)
        self.assertAlmostEqual(float(result["units_per_ke"]), 1.4)
        self.assertAlmostEqual(float(result["carb_units"]), 2.8)
        self.assertEqual(Decimal(result["target_mgdl"]), Decimal(100))
        self.assertEqual(Decimal(result["correction_units"]), Decimal("1.25"))
        self.assertAlmostEqual(float(result["total_units"]), 4.05)
        self.assertEqual(Decimal(result["entry_units"]), Decimal("4"))
        self.assertEqual(Decimal(result["cautious_entry_units"]), Decimal("3"))
        self.assertEqual(Decimal(result["expected_glucose_mgdl"]), Decimal("102"))
        self.assertEqual(Decimal(result["cautious_expected_glucose_mgdl"]), Decimal("142"))
        self.assertEqual(Decimal(result["cautious_target_mgdl"]), Decimal(140))
        self.assertEqual(Decimal(result["cautious_correction_units"]), Decimal("0.25"))
        self.assertAlmostEqual(float(result["cautious_total_units"]), 3.05)
        self.assertEqual(Decimal(result["trend_delta_mgdl"]), Decimal(30))
        self.assertEqual(result["trend_direction"], "up_fast")

    def test_half_unit_tie_rounds_to_next_injectable_amount(self):
        entries = [{"at": self.now, "sgv": 190}]
        result = dosing.suggest("20", self.settings, entries, [], self.now)
        self.assertAlmostEqual(float(result["total_units"]), 5.05)
        self.assertEqual(Decimal(result["entry_units"]), Decimal("5"))
        entries[0]["sgv"] = 198
        result = dosing.suggest("20", self.settings, entries, [], self.now)
        self.assertAlmostEqual(float(result["total_units"]), 5.25)
        self.assertEqual(Decimal(result["entry_units"]), Decimal("5.5"))
        self.assertEqual(Decimal(result["expected_glucose_mgdl"]), Decimal("90"))

    def test_configured_target_and_zero_floor(self):
        settings = {**self.settings, "dose_target_mgdl": "120"}
        result = dosing.suggest("0", settings,
                                [{"at": self.now, "sgv": 100}], [], self.now)
        self.assertEqual(Decimal(result["target_mgdl"]), Decimal(120))
        self.assertEqual(Decimal(result["correction_units"]), Decimal("-0.5"))
        self.assertEqual(Decimal(result["total_units"]), Decimal(0))
        self.assertEqual(Decimal(result["cautious_total_units"]), Decimal(0))

    def test_sport_glucose_change_shifts_dose_through_correction_factor(self):
        entries = [{"at": self.now, "sgv": 150}]
        base = dosing.suggest("20", self.settings, entries, [], self.now)
        lowered = dosing.suggest("20", self.settings, entries, [], self.now, activity_change="-40")
        self.assertEqual(Decimal(lowered["activity_change_mgdl"]), Decimal(-40))
        self.assertEqual(Decimal(lowered["activity_units"]), Decimal(-1))
        self.assertEqual(Decimal(base["total_units"]) - Decimal(lowered["total_units"]), Decimal(1))
        self.assertEqual(Decimal(lowered["entry_units"]), Decimal("3"))
        self.assertEqual(Decimal(lowered["cautious_entry_units"]), Decimal("2"))
        self.assertEqual(Decimal(lowered["expected_glucose_mgdl"]), Decimal("102"))
        raised = dosing.suggest("20", self.settings, entries, [], self.now, activity_change="40")
        self.assertEqual(Decimal(raised["entry_units"]), Decimal("5"))
        for invalid in ("NaN", "-501", "501"):
            with self.assertRaises(ValueError):
                dosing.suggest("20", self.settings, entries, [], self.now, activity_change=invalid)

    def test_configured_cautious_target_and_auto_factor_correct_separately(self):
        settings = {**self.settings, "dose_cautious_target_mgdl": "160"}
        entries = [{"at": self.now, "sgv": 150}]
        result = dosing.suggest("20", settings, entries, [], self.now, auto_units_per_ke=Decimal("1.05"))
        self.assertEqual(Decimal(result["cautious_target_mgdl"]), Decimal(160))
        self.assertEqual(Decimal(result["cautious_correction_units"]), Decimal("-0.25"))
        # 2 KE × 1.05 IE/KE + (150 − 100) / 40 = 2.1 + 1.25
        self.assertEqual(Decimal(result["auto_carb_units"]), Decimal("2.1"))
        self.assertEqual(Decimal(result["auto_total_units"]), Decimal("3.35"))
        self.assertEqual(Decimal(result["auto_entry_units"]), Decimal("3.5"))
        self.assertEqual(Decimal(result["auto_expected_glucose_mgdl"]), Decimal("94"))
        self.assertNotIn("auto_entry_units", dosing.suggest("20", settings, entries, [], self.now))

    def test_auto_factor_cautious_target_and_carbs_only_variants(self):
        entries = [{"at": self.now, "sgv": 150}]
        result = dosing.suggest("20", self.settings, entries, [], self.now, auto_units_per_ke=Decimal("1.05"))
        # 2.1 + (150 − 140) / 40 = 2.35 → 2.5 IE; 150 + (2.1 − 2.5) × 40 = 134
        self.assertEqual(Decimal(result["auto_cautious_total_units"]), Decimal("2.35"))
        self.assertEqual(Decimal(result["auto_cautious_entry_units"]), Decimal("2.5"))
        self.assertEqual(Decimal(result["auto_cautious_expected_glucose_mgdl"]), Decimal("134"))
        # Carbs only: 2.1 → 2 IE, no correction of the current 150 mg/dL.
        self.assertEqual(Decimal(result["auto_carbs_total_units"]), Decimal("2.1"))
        self.assertEqual(Decimal(result["auto_carbs_entry_units"]), Decimal("2"))
        self.assertEqual(Decimal(result["auto_carbs_expected_glucose_mgdl"]), Decimal("154"))
        self.assertEqual(Decimal(result["carbs_entry_units"]), Decimal("3"))
        # Sport still counts in the carbs-only dose: 2.1 − 40 / 40 = 1.1 → 1 IE.
        sport = dosing.suggest("20", self.settings, entries, [], self.now, activity_change="-40",
                               auto_units_per_ke=Decimal("1.05"))
        self.assertEqual(Decimal(sport["auto_carbs_entry_units"]), Decimal("1"))
        self.assertEqual(Decimal(sport["auto_carbs_expected_glucose_mgdl"]), Decimal("154"))
        # Without a fresh CGM value only the carbs-only dose remains.
        stale = dosing.suggest("20", self.settings, [], [], self.now, auto_units_per_ke=Decimal("1.05"))
        self.assertEqual(Decimal(stale["auto_carbs_entry_units"]), Decimal("2"))
        self.assertNotIn("auto_carbs_expected_glucose_mgdl", stale)
        self.assertNotIn("auto_cautious_entry_units", stale)

    def test_stale_glucose_has_no_correction(self):
        entries = [{"at": self.now - timedelta(minutes=16), "sgv": 150}]
        result = dosing.suggest("20", self.settings, entries, [], self.now)
        self.assertIn("carb_units", result)
        self.assertNotIn("total_units", result)
        self.assertNotIn("cautious_total_units", result)
        self.assertFalse(result["glucose_fresh"])
        self.assertNotIn("expected_glucose_mgdl", result)

    def test_missing_nightscout_target_does_not_block_local_target(self):
        settings = {**self.settings, "target_schedule": []}
        result = dosing.suggest("10", settings,
                                [{"at": self.now, "sgv": 135}], [], self.now)
        self.assertEqual(result["glucose_mgdl"], "135")
        self.assertIn("total_units", result)


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
    def test_server_refuses_to_start_without_password(self):
        with patch.dict(os.environ, {"APP_PASSWORD": ""}):
            with patch.object(server.db, "init") as init:
                with self.assertRaisesRegex(SystemExit, "APP_PASSWORD"):
                    server.main()
                init.assert_not_called()

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
