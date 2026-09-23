import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.calculation import calculate_episode, interpolate_glucose, isf_at


def moment(hour, minute=0):
    return datetime(2026, 9, 23, hour, minute, tzinfo=timezone.utc)


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "timezone": "Europe/Berlin",
            "window_minutes": 180,
            "peak_minutes": 120,
            "duration_minutes": 300,
            "isf_schedule": [
                {"start_minute": 0, "mgdl_per_unit": "50"},
                {"start_minute": 12 * 60, "mgdl_per_unit": "40"},
            ],
        }

    def test_interpolation_uses_exact_time(self):
        entries = [{"at": moment(10), "sgv": 100}, {"at": moment(10, 10), "sgv": 120}]
        result = interpolate_glucose(entries, moment(10, 5))
        self.assertEqual(result["value"], Decimal(110))

    def test_split_bolus_and_late_correction_count_full_units(self):
        start = moment(10)
        end = moment(13)
        entries = [{"at": start, "sgv": 100}, {"at": end, "sgv": 120},
                   {"at": moment(15, 57), "sgv": 120}]
        treatments = [
            {"id": "prior", "at": moment(9), "insulin": 2, "role": "correction"},
            {"id": "first", "at": start, "insulin": 4, "role": "meal"},
            {"id": "split", "at": moment(11), "insulin": 2, "role": "meal-dose"},
            {"id": "correction", "at": moment(12, 30), "insulin": 1, "role": "correction"},
            {"id": "late", "at": moment(12, 57), "insulin": 3, "role": "correction"},
            {"id": "at_end", "at": moment(15, 57), "insulin": 9, "role": "correction"},
        ]
        result = calculate_episode({"at": start, "carbs": 60}, treatments, entries, self.settings)
        self.assertEqual(result["end"], moment(15, 57).isoformat())
        self.assertEqual(result["end_reason"], "after_last_injection")
        self.assertEqual([dose["id"] for dose in result["doses"]], ["first", "split", "correction", "late"])
        self.assertEqual(Decimal(result["rapid_units_in_window"]), Decimal(10))
        self.assertEqual(Decimal(result["insulin_effect_mgdl"]), Decimal(400))
        self.assertEqual(Decimal(result["factor_units_per_ke"]), Decimal("1.75"))
        self.assertEqual(Decimal(result["doses"][-1]["effect_mgdl"]), Decimal(120))

    def test_prior_insulin_is_excluded(self):
        start = moment(10)
        entries = [{"at": start, "sgv": 100}, {"at": moment(13), "sgv": 100}]
        result = calculate_episode({"at": start, "carbs": 30},
            [{"id": "old", "at": moment(9), "insulin": 2, "role": "correction"}], entries, self.settings)
        self.assertEqual(result["doses"], [])
        self.assertIsNone(result["factor_units_per_ke"])

    def test_time_specific_isf_uses_berlin_local_clock(self):
        self.assertEqual(isf_at(moment(10), self.settings["isf_schedule"], "Europe/Berlin"), Decimal(40))
        self.assertEqual(isf_at(moment(8), self.settings["isf_schedule"], "Europe/Berlin"), Decimal(50))

    def test_nonpositive_denominator_has_no_factor(self):
        start = moment(10)
        entries = [{"at": start, "sgv": 150}, {"at": moment(13), "sgv": 100}]
        result = calculate_episode({"at": start, "carbs": 30}, [], entries, self.settings)
        self.assertIsNone(result["ratio_g_per_unit"])

    def test_additional_carbs_join_the_meal_window(self):
        start = moment(10)
        entries = [{"at": start, "sgv": 100}, {"at": moment(13), "sgv": 120}]
        meal = {"id": "meal", "source_id": "meal", "at": start, "carbs": 60}
        treatments = [
            {"id": "meal", "at": start, "carbs": 60, "insulin": 4, "eventType": "Meal Bolus"},
            {"id": "snack", "at": moment(11), "carbs": 15, "insulin": 0, "eventType": "Carb Correction"},
        ]
        result = calculate_episode(meal, treatments, entries, self.settings)
        self.assertIsNotNone(result["factor_units_per_ke"])
        self.assertEqual(Decimal(result["carbs_total"]), Decimal(75))
        self.assertEqual(Decimal(result["carbs_times_isf_total"]), Decimal(75 * 40))

    def test_unknown_insulin_type_suppresses_factor(self):
        start = moment(10)
        entries = [{"at": start, "sgv": 100}, {"at": moment(13), "sgv": 120}]
        result = calculate_episode({"at": start, "carbs": 60},
            [{"id": "basal", "at": start, "insulin": 10, "eventType": "Basal Injection"}], entries, self.settings)
        self.assertIsNone(result["factor_units_per_ke"])
        self.assertIn("unbekannten Typs", result["reason"])

    def test_snack_joins_single_factor_with_time_specific_isf(self):
        start = moment(8)
        entries = [{"at": start, "sgv": 100}, {"at": moment(11), "sgv": 120},
                   {"at": moment(13), "sgv": 120}]
        meal = {"id": "meal", "source_id": "meal", "at": start, "carbs": 50}
        treatments = [
            {"id": "meal", "at": start, "carbs": 50, "insulin": 4, "eventType": "Meal Bolus"},
            {"id": "snack", "at": moment(10), "carbs": 10, "insulin": 1, "eventType": "Snack Bolus"},
        ]
        result = calculate_episode(meal, treatments, entries, self.settings)
        self.assertIsNotNone(result["factor_units_per_ke"])
        self.assertEqual(Decimal(result["carbs_total"]), Decimal(60))
        self.assertEqual(Decimal(result["carbs_times_isf_total"]), Decimal(50 * 50 + 10 * 40))
        self.assertAlmostEqual(float(Decimal(result["factor_units_per_ke"]) * Decimal(result["ratio_g_per_unit"])), 10)

    def test_second_main_meal_blocks_window_without_premeal_cgm(self):
        start = moment(10)
        entries = [{"at": start, "sgv": 100}, {"at": moment(13), "sgv": 120}]
        meal = {"id": "meal", "source_id": "meal", "at": start, "carbs": 50}
        treatments = [
            {"id": "meal", "at": start, "carbs": 50, "insulin": 4, "eventType": "Meal Bolus"},
            {"id": "next", "at": moment(12, 58), "carbs": 100, "insulin": 6, "eventType": "Meal Bolus"},
        ]
        result = calculate_episode(meal, treatments, entries, self.settings)
        self.assertIsNone(result["factor_units_per_ke"])
        self.assertIn("Weitere Hauptmahlzeit", result["reason"])

    def test_next_main_meal_after_two_hours_uses_last_prior_cgm_point(self):
        start = moment(10)
        entries = [{"at": start, "sgv": 100}, {"at": moment(12, 57), "sgv": 120},
                   {"at": moment(13), "sgv": 180}]
        meal = {"id": "meal", "source_id": "meal", "at": start, "carbs": 50}
        treatments = [
            {"id": "meal", "at": start, "carbs": 50, "insulin": 4, "eventType": "Meal Bolus"},
            {"id": "next", "at": moment(12, 58), "carbs": 100, "insulin": 6, "eventType": "Meal Bolus"},
        ]
        result = calculate_episode(meal, treatments, entries, self.settings)
        self.assertEqual(result["end"], moment(12, 57).isoformat())
        self.assertEqual(result["end_reason"], "before_next_meal")
        self.assertEqual(result["g3"], "120")
        self.assertIsNotNone(result["factor_units_per_ke"])
        self.assertEqual([dose["id"] for dose in result["doses"]], ["meal"])

    def test_next_meal_bounds_window_extended_by_correction(self):
        start = moment(10)
        prior_meal_point = moment(13, 29)
        entries = [{"at": start, "sgv": 100}, {"at": prior_meal_point, "sgv": 120}]
        meal = {"id": "meal", "source_id": "meal", "at": start, "carbs": 50}
        treatments = [
            {"id": "meal", "at": start, "carbs": 50, "insulin": 4, "eventType": "Meal Bolus"},
            {"id": "correction", "at": moment(12, 30), "insulin": 1, "eventType": "Correction Bolus"},
            {"id": "next", "at": moment(13, 30), "carbs": 60, "insulin": 5, "eventType": "Meal Bolus"},
        ]
        result = calculate_episode(meal, treatments, entries, self.settings)
        self.assertEqual(result["end"], prior_meal_point.isoformat())
        self.assertEqual(result["end_reason"], "before_next_meal")
        self.assertEqual([dose["id"] for dose in result["doses"]], ["meal", "correction"])
        self.assertIsNotNone(result["factor_units_per_ke"])

    def test_next_main_meal_before_two_hours_still_blocks_factor(self):
        start = moment(10)
        entries = [{"at": start, "sgv": 100}, {"at": moment(11, 58), "sgv": 120},
                   {"at": moment(13), "sgv": 180}]
        meal = {"id": "meal", "source_id": "meal", "at": start, "carbs": 50}
        treatments = [
            {"id": "meal", "at": start, "carbs": 50, "insulin": 4, "eventType": "Meal Bolus"},
            {"id": "next", "at": moment(11, 59), "carbs": 100, "insulin": 6, "eventType": "Meal Bolus"},
        ]
        result = calculate_episode(meal, treatments, entries, self.settings)
        self.assertIsNone(result["factor_units_per_ke"])
        self.assertEqual(result["end_reason"], "fixed_window")

    def test_cgm_gap_rejected(self):
        entries = [{"at": moment(10), "sgv": 100}, {"at": moment(10, 20), "sgv": 120}]
        with self.assertRaisesRegex(ValueError, "CGM-Lücke"):
            interpolate_glucose(entries, moment(10, 5))


if __name__ == "__main__":
    unittest.main()
