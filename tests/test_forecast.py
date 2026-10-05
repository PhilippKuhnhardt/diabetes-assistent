import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app import forecast, server

TODAY = date(2026, 10, 4)
KEY = ("dinner", None)


def point(days_ago, value, meal="dinner", hour=18, day_type=None):
    day = TODAY - timedelta(days=days_ago)
    at = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return {"id": f"{meal}-{days_ago}", "day": day, "at": at, "key": (meal, day_type), "value": Decimal(value)}


def predict(points, percentile="37.5", half_life=7, lookback=28):
    return forecast.predict(points, KEY, TODAY, Decimal(percentile), Decimal(half_life), lookback)


class WeightedQuantileTests(unittest.TestCase):
    def test_equal_weights_match_the_analysis_quartile(self):
        values = [Decimal(value) for value in ("0.8", "1.0", "1.1", "1.2", "1.5")]
        for fraction in ("0.25", "0.375", "0.5", "0.75"):
            self.assertEqual(forecast.weighted_quantile([(value, Decimal(1)) for value in values], Decimal(fraction)),
                             server.quartile(values, Decimal(fraction)))

    def test_one_extreme_recent_day_moves_the_suggestion_by_at_most_one_place(self):
        history = [point(age, "1.0") for age in range(2, 12)] + [point(age, "1.2") for age in range(12, 20)]
        self.assertEqual(predict(history)["units_per_ke"], Decimal("1.0"))
        self.assertEqual(predict(history + [point(1, "3.0")])["units_per_ke"], Decimal("1.0"))

    def test_shorter_half_life_follows_recent_meals_faster(self):
        # Two weeks of 1.4 IE/KE, then a week at 1.0.
        history = [point(age, "1.0") for age in range(1, 8)] + [point(age, "1.4") for age in range(8, 22)]
        strong = predict(history, "50", 3)["units_per_ke"]
        weak = predict(history, "50", 14)["units_per_ke"]
        self.assertLess(strong, weak)

    def test_lookback_leaves_out_older_meals(self):
        history = [point(age, "1.0") for age in range(1, 15)] + [point(age, "2.0") for age in range(15, 29)]
        self.assertEqual(predict(history, lookback=14)["count"], 14)
        self.assertEqual(predict(history, lookback=14)["units_per_ke"], Decimal("1.0"))
        self.assertEqual(predict(history, lookback=28)["count"], 28)

    def test_today_and_too_few_meals_give_no_value(self):
        history = [point(0, "1.0")] + [point(age, "1.0") for age in range(1, 4)]
        result = predict(history)
        self.assertEqual(result["count"], 3)
        self.assertIsNone(result["units_per_ke"])


class BacktestTests(unittest.TestCase):
    def test_counts_where_the_endpoint_glucose_would_have_landed(self):
        points = [point(age, "1.0") for age in range(2, 20)]
        for item, value in zip(points[:4], ("1.2", "1.3", "1.4", "1.5")):
            item["value"] = Decimal(value)
        target = point(1, "1.0")
        # 40 g at KF 50: Σ[g × KF] = 2000, so 0.1 IE/KE more lowers the endpoint by 20 mg/dL.
        target.update({"end_glucose": Decimal(150), "carbs_isf": Decimal(2000)})
        result = forecast.backtest(points + [target], TODAY - timedelta(days=1), [(28, Decimal(7), Decimal(100))])
        row = result["rows"][0]
        self.assertEqual(row["predictions"], [Decimal("1.5")])
        # 0.5 IE/KE too much: 150 − 0.5 × 2000 / 10 = 50 mg/dL, below the range.
        self.assertEqual(row["glucose"], [Decimal(50)])
        stats = result["stats"][0]
        self.assertEqual((stats["below"], stats["in_range"], stats["above"], stats["glucose_count"]), (1, 0, 0, 1))

    def test_all_variants_are_compared_on_the_same_meals(self):
        # Meals 16–27 days ago only reach the 28-day lookback, so 14-day predictions need recent meals.
        points = [point(age, "1.0") for age in range(1, 4)] + [point(age, "1.0") for age in range(16, 28)]
        variants = [(14, Decimal(7), Decimal(50)), (28, Decimal(7), Decimal(50))]
        result = forecast.backtest(points, TODAY - timedelta(days=3), variants)
        self.assertEqual(result["stats"][0]["count"], result["stats"][1]["count"])
        self.assertEqual(result["stats"][0]["count"], 0)
        alone = forecast.backtest(points, TODAY - timedelta(days=3), variants[1:])
        self.assertEqual(alone["stats"][0]["count"], 3)

    def test_rows_without_endpoint_glucose_are_left_out_of_the_ranges(self):
        points = [point(age, "1.0") for age in range(1, 20)]
        stats = forecast.backtest(points, TODAY - timedelta(days=2), [(28, Decimal(7), Decimal(50))])["stats"][0]
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["glucose_count"], 0)
        self.assertIsNone(stats["mean_glucose"])
        self.assertIsNone(stats["median_glucose"])

    def test_mean_and_median_of_the_simulated_endpoint_glucose(self):
        points = [point(age, "1.0") for age in range(4, 20)]
        # The prediction is 1.0 IE/KE for every target, so each endpoint stays as measured.
        for age, glucose in ((1, 90), (2, 120), (3, 210)):
            target = point(age, "1.0")
            target.update({"end_glucose": Decimal(glucose), "carbs_isf": Decimal(2000)})
            points.append(target)
        stats = forecast.backtest(points, TODAY - timedelta(days=3), [(28, Decimal(7), Decimal(50))])["stats"][0]
        self.assertEqual((stats["mean_glucose"], stats["median_glucose"]), (Decimal(140), Decimal(120)))


class DayTypeRatioTests(unittest.TestCase):
    def breakfast(self, days_ago, value):
        item = point(days_ago, value, meal="breakfast", hour=8)
        kind = "free" if item["day"].weekday() >= 5 else "work"
        item.update({"key": ("breakfast", kind), "day_type": kind})
        return item

    def test_stress_surcharge_against_the_two_work_days_before_and_after(self):
        # Work days around the weekend 14 and 15 days ago need 1.5 IE/KE, all others 1.0; Wednesdays
        # are odd (3.0) but too far from any weekend to count. Free days are always 0.8 × their neighbours,
        # so work days carry a stress surcharge of 1 / 0.8 − 1 = 25 %.
        points = []
        for age in range(0, 40):
            day = TODAY - timedelta(days=age)
            level = Decimal("1.5") if 12 <= age <= 17 else Decimal("1.0")
            if day.weekday() >= 5:
                value = level * Decimal("0.8")
            else:
                value = Decimal("3.0") if day.weekday() == 2 else level
            points.append(self.breakfast(age, value))
        result = forecast.day_type_ratios(points, TODAY)["meals"]["breakfast"]
        self.assertEqual((result["single_median"], result["single_min"], result["single_max"]),
                         (Decimal("0.25"), Decimal("0.25"), Decimal("0.25")))
        # Saturdays and Sundays within 30 days (today is a Sunday, so the last weekend only has days before).
        self.assertEqual(len(result["singles"]), 10)
        self.assertEqual((result["singles"][-1]["before"], result["singles"][-1]["after"]), (2, 0))
        self.assertEqual((result["singles"][0]["before"], result["singles"][0]["after"]), (2, 2))
        self.assertEqual(result["free_count"], 10)

    def test_without_free_days_there_is_no_surcharge(self):
        points = [dict(point(age, "1.0"), day_type="work") for age in range(1, 10)]
        result = forecast.day_type_ratios(points, TODAY)["meals"]["dinner"]
        self.assertEqual((result["work_count"], result["free_count"]), (9, 0))
        self.assertIsNone(result["surcharge"])
        self.assertEqual(result["singles"], [])


if __name__ == "__main__":
    unittest.main()
