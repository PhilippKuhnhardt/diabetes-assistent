import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app import forecast, server

TODAY = date(2026, 10, 4)
NOW = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)


def point(days_ago, value, meal="dinner", hour=18, day_type=None, ended=True):
    day = TODAY - timedelta(days=days_ago)
    at = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return {"id": f"{meal}-{days_ago}", "day": day, "at": at, "end": at + timedelta(hours=3) if ended else None,
            "key": (meal, day_type), "value": Decimal(value)}


class WeightedQuantileTests(unittest.TestCase):
    def test_equal_weights_match_the_analysis_quartile(self):
        values = [Decimal(value) for value in ("0.8", "1.0", "1.1", "1.2", "1.5")]
        for fraction in ("0.25", "0.375", "0.5", "0.75"):
            self.assertEqual(forecast.weighted_quantile([(value, Decimal(1)) for value in values], Decimal(fraction)),
                             server.quartile(values, Decimal(fraction)))

    def test_one_extreme_recent_day_moves_the_suggestion_by_at_most_one_place(self):
        history = [point(age, "1.0") for age in range(2, 12)] + [point(age, "1.2") for age in range(12, 20)]
        calm = forecast.baseline(history, ("dinner", None), TODAY, Decimal("0.375"), "weighted", Decimal(7))
        odd = forecast.baseline(history + [point(1, "3.0")], ("dinner", None), TODAY,
                                Decimal("0.375"), "weighted", Decimal(7))
        self.assertEqual(calm, Decimal("1.0"))
        self.assertEqual(odd, Decimal("1.0"))

    def test_recent_meals_count_more_than_old_ones(self):
        # Two weeks of 1.4 IE/KE, then a week at 1.0: the flat rule still mixes in old values.
        history = ([point(age, "1.0") for age in range(1, 8)] +
                   [point(age, "1.4") for age in range(8, 22)])
        key = ("dinner", None)
        flat = forecast.baseline(history, key, TODAY, Decimal("0.5"), "flat", Decimal(7))
        weighted = forecast.baseline(history, key, TODAY, Decimal("0.5"), "weighted", Decimal(7))
        self.assertEqual(flat, Decimal("1.4"))
        self.assertLess(weighted, Decimal("1.4"))

    def test_too_few_meals_give_no_baseline(self):
        history = [point(age, "1.0") for age in range(1, 4)]
        self.assertIsNone(forecast.baseline(history, ("dinner", None), TODAY, Decimal("0.375"), "weighted", Decimal(7)))


class DayShiftTests(unittest.TestCase):
    def setUp(self):
        # Four weeks of steady factors for every meal kind, so each reference median is 1.0.
        self.history = [point(age, "1.0", meal, hour) for age in range(2, 29)
                        for meal, hour in (("breakfast", 7), ("lunch", 12), ("dinner", 18))]
        for item in self.history:
            if item["key"][0] == "breakfast":
                item["key"] = ("breakfast", "work")

    def recent(self, *values):
        meals = (("breakfast", 7, "work"), ("lunch", 12, None), ("dinner", 18, None))
        result = []
        for (meal, hour, day_type), value in zip(meals, values):
            item = point(1, value, meal, hour, day_type)
            result.append(item)
        return result

    def test_three_higher_meals_raise_by_the_smallest_deviation(self):
        shift = forecast.day_shift(self.history + self.recent("1.1", "1.2", "1.08"), NOW, Decimal(7))
        self.assertTrue(shift["applied"])
        self.assertEqual(shift["reason"], "higher")
        self.assertEqual(shift["factor"], Decimal("1.08"))

    def test_mixed_directions_change_nothing(self):
        shift = forecast.day_shift(self.history + self.recent("1.3", "0.9", "1.2"), NOW, Decimal(7))
        self.assertFalse(shift["applied"])
        self.assertEqual(shift["factor"], Decimal(1))

    def test_shift_is_capped(self):
        shift = forecast.day_shift(self.history + self.recent("0.5", "0.6", "0.7"), NOW, Decimal(7))
        self.assertEqual(shift["factor"], Decimal("0.85"))

    def test_unfinished_and_old_meals_do_not_count(self):
        recent = self.recent("1.2", "1.2", "1.2")
        recent[-1]["end"] = NOW + timedelta(minutes=30)
        shift = forecast.day_shift(self.history + recent, NOW, Decimal(7))
        # The dinner still running is skipped; an older finished meal takes its place.
        self.assertTrue(all(meal["id"] != recent[-1]["id"] for meal in shift["meals"]))

    def test_prediction_multiplies_baseline_and_shift(self):
        points = self.history + self.recent("1.2", "1.2", "1.2")
        result = forecast.predict(points, ("dinner", None), NOW, TODAY, Decimal("37.5"),
                                  "weighted_shift", Decimal(7))
        self.assertEqual(result["baseline_units_per_ke"], Decimal("1.0"))
        self.assertEqual(result["units_per_ke"], Decimal("1.15"))
        plain = forecast.predict(points, ("dinner", None), NOW, TODAY, Decimal("37.5"), "weighted", Decimal(7))
        self.assertEqual(plain["units_per_ke"], Decimal("1.0"))
        self.assertIsNone(plain["shift"])


class BacktestTests(unittest.TestCase):
    def test_compares_each_method_with_the_calculated_factor(self):
        points = [point(age, "1.0") for age in range(5, 20)] + [point(age, "1.3") for age in range(1, 5)]
        result = forecast.backtest(points, TODAY - timedelta(days=4), Decimal("50"), Decimal(7))
        self.assertEqual(len(result["rows"]), 4)
        for method in forecast.METHODS:
            stats = result["stats"][method]
            self.assertEqual(stats["count"], 4)
            self.assertEqual(stats["hits"] + stats["too_much"] + stats["too_little"], 4)
        # Every method predicted about 1.0 for meals that needed 1.3: too little insulin, not too much.
        self.assertEqual(result["stats"]["flat"]["too_little"], 4)
        self.assertEqual(result["stats"]["flat"]["too_much"], 0)

    def test_counts_where_the_endpoint_glucose_would_have_landed(self):
        points = [point(age, "1.0") for age in range(2, 20)]
        target = point(1, "1.0")
        # 40 g at KF 50: Σ[g × KF] = 2000, so 0.1 IE/KE more lowers the endpoint by 20 mg/dL.
        target.update({"end_glucose": Decimal(150), "carbs_isf": Decimal(2000),
                       "target_low": Decimal(80), "target_high": Decimal(160)})
        for item, value in zip(points[:4], ("1.2", "1.3", "1.4", "1.5")):
            item["value"] = Decimal(value)
        result = forecast.backtest(points + [target], TODAY - timedelta(days=1), Decimal("100"), Decimal(7))
        row = result["rows"][0]
        prediction = row["predictions"]["flat"]
        self.assertEqual(prediction, Decimal("1.5"))
        # 0.5 IE/KE too much: 150 − 0.5 × 2000 / 10 = 50 mg/dL, below the range.
        self.assertEqual(row["glucose"]["flat"], Decimal(50))
        stats = result["stats"]["flat"]
        self.assertEqual((stats["below"], stats["in_range"], stats["above"], stats["glucose_count"]), (1, 0, 0, 1))

    def test_rows_without_endpoint_glucose_are_left_out_of_the_ranges(self):
        points = [point(age, "1.0") for age in range(1, 20)]
        stats = forecast.backtest(points, TODAY - timedelta(days=2), Decimal("50"), Decimal(7))["stats"]["flat"]
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["glucose_count"], 0)


if __name__ == "__main__":
    unittest.main()
