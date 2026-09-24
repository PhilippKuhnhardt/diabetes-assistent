import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.sensitivity import compare_schedule


class SensitivityComparisonTests(unittest.TestCase):
    def test_candidate_reuses_displayed_deviations_for_all_meals(self):
        start = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
        exposures = [Decimal("1"), Decimal("-1"), Decimal("2"), Decimal("-2"),
                     Decimal("1.5"), Decimal("-1.5"), Decimal("3"), Decimal("-3")]
        meals = []
        for index, exposure in enumerate(exposures):
            at = start + timedelta(days=index)
            # 50 g at 10 g/IE gives 5 IE of carb coverage.
            units = Decimal(5) - exposure
            meals.append({"at": at, "excluded": False,
                          "deviation_mgdl": str(exposure * 10), "calculation": {
                "carb_events": [{"at": at.isoformat(), "carbs": "50"}],
                "doses": [{"at": at.isoformat(), "units": str(units)}],
            }})
        result = compare_schedule(meals, [{"start_minute": 0, "mgdl_per_unit": "30"}],
                                  [{"start_minute": 0, "grams_per_unit": "10"}], "UTC")[0]
        self.assertEqual(Decimal(result["candidate_mgdl_per_unit"]), Decimal(40))
        self.assertEqual(Decimal(result["candidate_mae_mgdl"]), Decimal(0))
        self.assertGreater(Decimal(result["baseline_mae_mgdl"]), 0)
        self.assertEqual(result["meal_count"], 8)

    def test_single_small_exposure_is_included(self):
        at = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
        meals = [{"at": at, "excluded": False, "deviation_mgdl": "1", "calculation": {
            "carb_events": [{"at": at.isoformat(), "carbs": "55"}],
            "doses": [{"at": at.isoformat(), "units": "4.3"}],
        }}]
        result = compare_schedule(meals, [{"start_minute": 0, "mgdl_per_unit": "30"}],
                                  [{"start_minute": 0, "grams_per_unit": "12.5"}], "UTC")[0]
        self.assertEqual(Decimal(result["candidate_mgdl_per_unit"]), Decimal(40))
        self.assertEqual(result["meal_count"], 1)

    def test_balanced_meal_cannot_change_its_deviation(self):
        at = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
        meals = [{"at": at, "excluded": False, "deviation_mgdl": "30", "calculation": {
            "carb_events": [{"at": at.isoformat(), "carbs": "55"}],
            "doses": [{"at": at.isoformat(), "units": "4.4"}],
        }}]
        result = compare_schedule(meals, [{"start_minute": 0, "mgdl_per_unit": "30"}],
                                  [{"start_minute": 0, "grams_per_unit": "12.5"}], "UTC")[0]
        self.assertIsNone(result["candidate_mgdl_per_unit"])
        self.assertEqual(result["meal_count"], 0)


if __name__ == "__main__":
    unittest.main()
