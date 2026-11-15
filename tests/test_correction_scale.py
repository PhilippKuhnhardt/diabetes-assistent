import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import correction_scale

MEAL_AT = datetime(2026, 11, 14, 11, 0, tzinfo=timezone.utc)
MEAL = {"at": MEAL_AT, "role": "meal", "carbs": "80", "insulin": "6", "eventType": "Meal Bolus"}


def readings(per_minute, minutes=200, start=120):
    """CGM every 5 min rising linearly for the first hour, then flat."""
    return [{"at": MEAL_AT + timedelta(minutes=m), "sgv": start + per_minute * min(m, 60)}
            for m in range(0, minutes, 5)]


class MealRiseTests(unittest.TestCase):
    def rise(self, per_minute, after=150, treatments=None, entries=None):
        return correction_scale.meal_rise(entries if entries is not None else readings(per_minute),
                                          treatments if treatments is not None else [MEAL],
                                          MEAL_AT + timedelta(minutes=after))

    def test_steepest_slope_in_the_first_hour_sets_the_tempo(self):
        self.assertEqual(self.rise(3)["tempo"], "fast")
        self.assertEqual(self.rise(Decimal("1.5"))["tempo"], "normal")
        self.assertEqual(self.rise(Decimal("0.5"))["tempo"], "slow")
        self.assertAlmostEqual(float(self.rise(3)["slope_mgdl_per_min"]), 3.0, places=6)

    def test_only_from_one_hour_until_seven_and_a_half_hours_after_a_main_meal(self):
        self.assertIsNone(self.rise(3, after=55))
        self.assertIsNotNone(self.rise(3, after=60))
        self.assertIsNone(self.rise(3, after=451))
        self.assertIsNone(self.rise(3, treatments=[{**MEAL, "role": "snack", "eventType": "Carb Correction"}]))

    def test_without_cgm_the_fast_mark_stands_in(self):
        self.assertIsNone(self.rise(3, entries=[])["tempo"])
        self.assertEqual(self.rise(3, entries=[], treatments=[{**MEAL, "fast": True}])["tempo"], "fast")


class ScaleTests(unittest.TestCase):
    RESULT = {"forecast_mgdl": "220", "correction_units": "3", "entry_units": "3", "isf_mgdl_per_unit": "33.3"}
    RISE = {"meal_at": MEAL_AT, "slope_mgdl_per_min": Decimal("2.4"), "tempo": "fast"}

    def test_ends_are_the_quartiles_of_what_was_right(self):
        scale = correction_scale.scale(self.RESULT, self.RISE)
        self.assertEqual(scale["fast_entry_units"], "2")       # 3 x 0.6 = 1.8 -> 2
        self.assertEqual(scale["slow_entry_units"], "4")       # 3 x 1.4 = 4.2 -> 4
        self.assertEqual(Decimal(scale["fast_expected_glucose_mgdl"]), Decimal("220") - 2 * Decimal("33.3"))
        self.assertEqual(scale["tempo"], "fast")

    def test_low_end_is_at_least_one_unit_below_normal(self):
        scale = correction_scale.scale({**self.RESULT, "correction_units": "2", "entry_units": "2"}, self.RISE)
        self.assertEqual(scale["fast_entry_units"], "1")       # min(1.2, 1) -> 1
        self.assertEqual(scale["slow_entry_units"], "3")       # 2.8 -> 3, no gap upwards
        small = correction_scale.scale({**self.RESULT, "correction_units": "0.8", "entry_units": "1"}, self.RISE)
        self.assertEqual(small["fast_entry_units"], "0")

    def test_no_scale_without_a_positive_correction_or_a_recent_meal(self):
        self.assertIsNone(correction_scale.scale({**self.RESULT, "correction_units": "-0.5"}, self.RISE))
        self.assertIsNone(correction_scale.scale(self.RESULT, None))
        # Normal rounds to 0 IE (e.g. right after a correction): no three zeros.
        self.assertIsNone(correction_scale.scale({**self.RESULT, "correction_units": "0.17", "entry_units": "0"}, self.RISE))
        self.assertIsNone(correction_scale.scale({"correction_units": "3"}, self.RISE))


if __name__ == "__main__":
    unittest.main()
