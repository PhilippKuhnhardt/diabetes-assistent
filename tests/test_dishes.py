import unittest

from app import dishes


def meal(day, factor, dish_id=None, kind="breakfast", excluded=False, delta=10, end=120):
    return {"day": day, "meal": kind, "meal_id": day + kind, "at": day + "T08:00+02:00", "dish_id": dish_id,
            "carbs_g": "50", "factor": factor, "g0_mgdl": "110", "g_end_mgdl": str(end), "delta_mgdl": str(delta),
            "excluded": excluded}


class DishRuleTests(unittest.TestCase):
    def test_names_are_trimmed_and_required(self):
        self.assertEqual(dishes.validate_name("  Porridge   mit Banane "), "Porridge mit Banane")
        with self.assertRaises(ValueError):
            dishes.validate_name("  ")

    def test_typical_carbs_is_the_median_of_the_last_entries(self):
        self.assertEqual(dishes.typical_carbs([90, 90, 50, 52, 48, 50, 55]), 50)
        self.assertIsNone(dishes.typical_carbs([None, 0]))

    def test_reference_is_the_usual_factor_of_the_meal_kind_before_the_meal(self):
        meals = [meal(f"2026-10-{day:02d}", "1.0") for day in range(1, 6)]
        meals += [meal("2026-10-03", "3.0", kind="lunch"), meal("2026-10-04", "9.0", excluded=True),
                  meal("2026-10-05", None)]
        target = meal("2026-10-06", "1.5", dish_id=1)
        self.assertEqual(dishes.reference_factor(meals + [target], target), 1.0)
        # Fewer than four earlier meals of the kind name no usual factor.
        self.assertIsNone(dishes.reference_factor(meals[:3] + [target], target))
        # Meals older than the lookback stay out.
        late = meal("2026-12-20", "1.5", dish_id=1)
        self.assertIsNone(dishes.reference_factor(meals + [late], late))

    def test_outcome_names_how_much_more_insulin_the_dish_needed(self):
        meals = [meal(f"2026-10-{day:02d}", "1.0") for day in range(1, 11)]
        meals += [meal(f"2026-10-{day}", factor, dish_id=7, kind="lunch")
                  for day, factor in ((11, "2.4"), (12, "2.6"), (13, "2.2"))]
        meals += [meal(f"2026-10-{day:02d}", "2.0", kind="lunch") for day in range(1, 11)]
        rows = dishes.history(meals, 7)
        self.assertEqual([row["ratio"] for row in rows], [1.2, 1.3, 1.1])
        result = dishes.outcome(rows, 50)
        self.assertEqual((result["meals"], result["valid"], result["ratio"]), (3, 3, 1.2))
        self.assertEqual((result["effective_carbs"], result["effective_low"], result["effective_high"]), (60, 55, 65))
        self.assertIn("20 % mehr Insulin als üblich", dishes.hint(3, 50, result))
        self.assertIn("55–65 g", dishes.hint(3, 50, result))

    def test_two_meals_name_no_ratio_yet(self):
        meals = [meal(f"2026-10-{day:02d}", "1.0") for day in range(1, 11)]
        meals += [meal("2026-10-11", "1.2", dish_id=7), meal("2026-10-12", "1.3", dish_id=7)]
        result = dishes.outcome(dishes.history(meals, 7), 50)
        self.assertEqual((result["valid"], result["ratio"], result["effective_carbs"]), (2, None, None))
        self.assertEqual(dishes.hint(2, 50, result), "2× eingetragen, meist 50 g")

    def test_excluded_and_blocked_meals_count_as_meals_but_not_into_the_ratio(self):
        meals = [meal(f"2026-10-{day:02d}", "1.0") for day in range(1, 11)]
        meals += [meal("2026-10-11", "1.2", dish_id=7, excluded=True), meal("2026-10-12", None, dish_id=7)]
        result = dishes.outcome(dishes.history(meals, 7), 50)
        self.assertEqual((result["meals"], result["valid"]), (2, 0))


if __name__ == "__main__":
    unittest.main()
