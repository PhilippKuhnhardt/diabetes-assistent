import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import dosing, episodes, forecast, on_board, split


class CurveTests(unittest.TestCase):
    def test_fiasp_curve_matches_the_approved_table(self):
        expected = {0: 1, 15: 1, 30: Decimal("0.94"), 60: Decimal("0.75"), 90: Decimal("0.54"),
                    120: Decimal("0.36"), 180: Decimal("0.14"), 240: Decimal("0.04"), 360: 0}
        for minutes, share in expected.items():
            with self.subTest(minutes):
                self.assertAlmostEqual(float(on_board.insulin_remaining(Decimal(minutes))), float(share), places=2)

    def test_carb_absorption_grows_with_amount(self):
        self.assertEqual(on_board.carb_minutes(Decimal(30)), 120)
        self.assertEqual(on_board.carb_minutes(Decimal(70)), 150)
        self.assertEqual(on_board.carb_minutes(Decimal(140)), 180)
        self.assertEqual(on_board.carb_remaining(Decimal(60), Decimal(30)), Decimal("0.5"))

    def test_surcharge_acts_from_two_to_five_hours(self):
        self.assertEqual(on_board.surcharge_remaining(Decimal(60)), 1)
        self.assertEqual(on_board.surcharge_remaining(Decimal(210)), Decimal("0.5"))
        self.assertEqual(on_board.surcharge_remaining(Decimal(300)), 0)
        self.assertEqual(on_board.surcharge_remaining(Decimal(300), fat=True), Decimal("0.25"))


class OnBoardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)  # 14:00 in Berlin
        self.settings = {
            "timezone": "Europe/Berlin",
            "ic_schedule": [{"start_minute": 0, "grams_per_unit": "10"}],
            "isf_schedule": [{"start_minute": 0, "mgdl_per_unit": "35"}],
            "dose_target_mgdl": "120", "dose_cautious_target_mgdl": "150",
        }

    def active(self, treatments, factor=Decimal("1.17"), surcharge=Decimal(40)):
        found = on_board.sources(treatments, self.now, self.settings, lambda at: factor, surcharge,
                                 forecast.surcharge_share)
        return on_board.summary(found, self.now)

    def test_fresh_correction_suppresses_another_one(self):
        doses = [{"id": "c", "at": self.now - timedelta(minutes=5), "eventType": "Correction Bolus", "insulin": 2}]
        active = self.active(doses)
        self.assertEqual(active["insulin_units"], 2)
        self.assertEqual(active["lowering_mgdl"], 70)
        result = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 170}], doses, self.now, on_board=active)
        self.assertEqual(Decimal(result["forecast_mgdl"]), 100)
        self.assertEqual(Decimal(result["entry_units"]), 0)
        self.assertEqual(Decimal(result["cautious_entry_units"]), 0)
        plain = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 170}], doses, self.now)
        self.assertEqual(Decimal(plain["entry_units"]), Decimal("1.5"))

    def test_older_correction_counts_only_its_rest(self):
        doses = [{"id": "c", "at": self.now - timedelta(hours=2), "eventType": "Correction Bolus", "insulin": 2}]
        result = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 170}], doses, self.now,
                                on_board=self.active(doses))
        self.assertEqual(Decimal(result["entry_units"]), Decimal("0.5"))

    def test_meal_rest_cancels_bolus_but_only_normal_adds_insulin_for_it(self):
        meal = [{"id": "m", "at": self.now - timedelta(hours=1), "eventType": "Meal Bolus", "carbs": 60,
                 "insulin": 7}]
        active = self.active(meal)
        self.assertAlmostEqual(float(active["insulin_units"]), 5.27, places=2)
        self.assertAlmostEqual(float(active["carbs_mgdl"]), 60 * (1 - 60 / 140) / 10 * 1.17 * 35, places=6)
        self.assertEqual(active["surcharge_mgdl"], 40)
        self.assertEqual(active["fresh_meal_at"], meal[0]["at"])
        result = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 170}], meal, self.now, on_board=active)
        self.assertEqual(Decimal(result["entry_units"]), Decimal("1.5"))  # forecast ≈ 166 → (166 − 120) / 35
        self.assertAlmostEqual(float(result["cautious_forecast_mgdl"]), 166, places=0)  # rests only cancel the bolus
        self.assertEqual(Decimal(result["cautious_entry_units"]), Decimal("0.5"))

    def test_cautious_counts_a_large_meal_rest_minus_the_typical_miss(self):
        # Like the evening of 2026-10-08 (144 g fat-rich dinner, split bolus, glucose 165 and rising), 1.5 h after it.
        meal = [{"id": "m", "at": self.now - timedelta(minutes=90), "eventType": "Meal Bolus", "carbs": 144,
                 "insulin": 8, "fat_protein": True}]
        active = self.active(meal, factor=Decimal("0.9"))
        result = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 165}], meal, self.now, on_board=active)
        full = 165 - active["lowering_mgdl"] + active["raising_mgdl"]
        self.assertEqual(Decimal(result["forecast_mgdl"]), full)
        self.assertEqual(Decimal(result["cautious_forecast_mgdl"]), full - on_board.FORECAST_SPREAD_MGDL)
        # Clearly more than the 0.5 IE of rests that only cancel the bolus (shorter carbs since 2026-10-22: 2 IE).
        self.assertGreaterEqual(Decimal(result["cautious_entry_units"]), 2)
        self.assertLess(Decimal(result["cautious_entry_units"]), Decimal(result["entry_units"]))

    def test_lower_target_corrects_the_same_forecast_only_while_little_acts(self):
        result = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 190}], [], self.now,
                                on_board=self.active([]))
        self.assertNotIn("low_blocked", result)
        self.assertEqual(Decimal(result["low_target_mgdl"]), 100)
        self.assertEqual(Decimal(result["entry_units"]), 2)              # (190 − 120) / 35
        self.assertEqual(Decimal(result["low_entry_units"]), Decimal("2.5"))  # (190 − 100) / 35 = 2.57
        self.assertEqual(Decimal(result["low_expected_glucose_mgdl"]), Decimal("102.5"))
        # 2 IE 40 min ago still lower by about 60 mg/dL: offered; 5 IE (≥ 150 mg/dL) are too much.
        doses = [{"id": "c", "at": self.now - timedelta(minutes=40), "eventType": "Correction Bolus", "insulin": 2}]
        some = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 250}], doses, self.now,
                              on_board=self.active(doses))
        self.assertNotIn("low_blocked", some)
        doses[0]["insulin"] = 5
        acting = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 300}], doses, self.now,
                                on_board=self.active(doses))
        self.assertEqual(acting["low_blocked"], "acting")
        # Two hours after the last dose the model's rest no longer holds the option back.
        later = self.now + timedelta(minutes=85)
        settled = dosing.suggest("0", self.settings, [{"at": later, "sgv": 300}], doses, later,
                                 on_board={"lowering_mgdl": Decimal(160), "raising_mgdl": Decimal(0)})
        self.assertNotIn("low_blocked", settled)
        evening = dosing.suggest("0", self.settings, [{"at": self.now + timedelta(hours=6), "sgv": 190}], [],
                                 self.now + timedelta(hours=6))   # 20:00 in Berlin
        self.assertEqual(evening["low_blocked"], "evening")
        falling = dosing.suggest("0", self.settings, [{"at": self.now - timedelta(minutes=10), "sgv": 220},
                                                      {"at": self.now, "sgv": 190}], [], self.now)
        self.assertEqual(falling["low_blocked"], "falling")
        low = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 65}], [], self.now)
        self.assertEqual(low["low_blocked"], "low")
        off = dosing.suggest("0", {**self.settings, "dose_low_target_mgdl": "120"},
                             [{"at": self.now, "sgv": 190}], [], self.now)
        self.assertEqual(off["low_blocked"], "off")

    def test_low_glucose_never_doses_for_carbs_still_to_come(self):
        meal = [{"id": "m", "at": self.now - timedelta(minutes=30), "eventType": "Meal Bolus", "carbs": 80,
                 "insulin": 2}]
        result = dosing.suggest("0", self.settings, [{"at": self.now, "sgv": 65}], meal, self.now,
                                on_board=self.active(meal))
        self.assertEqual(Decimal(result["entry_units"]), 0)
        self.assertEqual(result["forecast_mgdl"], result["cautious_forecast_mgdl"])

    def test_snacks_basal_and_old_entries_carry_no_effect(self):
        treatments = [
            {"id": "s", "at": self.now - timedelta(minutes=30), "eventType": "Snack Bolus", "carbs": 20, "insulin": 1},
            {"id": "b", "at": self.now - timedelta(minutes=30), "eventType": "Note", "basal_units": 17},
            {"id": "o", "at": self.now - timedelta(hours=7), "eventType": "Meal Bolus", "carbs": 90, "insulin": 9},
        ]
        active = self.active(treatments)
        self.assertEqual(active["surcharge_mgdl"], 0)
        self.assertEqual(active["insulin_units"], Decimal(1) * on_board.insulin_remaining(Decimal(30)))
        self.assertEqual([row["grams"] for row in active["carbs"]], [20])
        self.assertIsNone(active["fresh_meal_at"])

    def test_until_marks_when_each_side_has_acted(self):
        doses = [{"id": "c", "at": self.now, "eventType": "Correction Bolus", "insulin": 2}]
        active = self.active(doses)
        self.assertIsNone(active["raising_until"])
        self.assertTrue(self.now + timedelta(hours=3) < active["lowering_until"] < self.now + timedelta(hours=6))
        self.assertEqual(active["settled_at"], active["lowering_until"])


class ForecastBandTests(unittest.TestCase):
    def test_band_widens_with_what_still_acts(self):
        self.assertEqual(on_board.forecast_band(Decimal(0), Decimal(0)), (Decimal(30), Decimal(30)))
        self.assertEqual(on_board.forecast_band(Decimal(20), Decimal(19)), (Decimal(30), Decimal(30)))
        self.assertEqual(on_board.forecast_band(Decimal(30), Decimal(10)), (Decimal(50), Decimal(60)))
        self.assertEqual(on_board.forecast_band(Decimal(100), Decimal(60)), (Decimal(75), Decimal(90)))


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)
        self.settings = {"timezone": "Europe/Berlin", "isf_schedule": [{"start_minute": 0, "mgdl_per_unit": "30"}]}
        self.entries = [{"at": self.start + timedelta(minutes=5 * k), "sgv": 150} for k in range(-48, 120)]

    def rows(self, treatments):
        end = self.start + timedelta(hours=6)
        found = on_board.sources(treatments, end, self.settings, lambda at: Decimal(1), None,
                                 forecast.surcharge_share, keep_all=True)
        return on_board.timeline(found, self.entries, self.start, end)

    def test_without_events_the_model_is_the_glucose_three_hours_earlier(self):
        rows = self.rows([])
        self.assertTrue(all(row["model_mgdl"] == 150 and row["lowering_per_hour"] == 0 for row in rows))

    def test_rates_add_up_to_the_full_effect_and_the_model_follows_them(self):
        dose = self.start + timedelta(minutes=30)
        rows = self.rows([{"id": "c", "at": dose, "eventType": "Correction Bolus", "insulin": 2}])
        before = [row for row in rows if row["at"] < dose]
        self.assertTrue(all(row["lowering_per_hour"] == 0 for row in before))
        acted = sum(row["lowering_per_hour"] for row in rows) / 12
        self.assertAlmostEqual(float(acted), 60 * (1 - float(on_board.insulin_remaining(Decimal(335)))), places=3)
        three_hours = next(row for row in rows if row["at"] == dose + timedelta(hours=3))
        self.assertAlmostEqual(float(three_hours["model_mgdl"]), 150 - 60 * (1 - 0.14), delta=1)
        self.assertTrue(all(row["lowering_per_hour"] >= 0 and row["raising_per_hour"] == 0 for row in rows))


class SplitTests(unittest.TestCase):
    settings = {"split_min_grams": 80, "split_first_percent": 60, "split_delay_minutes": 120}

    def test_offered_for_large_main_meals_only(self):
        self.assertEqual(split.offer(Decimal(80), False, self.settings), {"first_percent": 60, "delay_minutes": 120})
        self.assertIsNone(split.offer(Decimal(79), False, self.settings))
        self.assertIsNone(split.offer(Decimal(120), True, self.settings))
        self.assertIsNone(split.offer(Decimal(120), False, {**self.settings, "split_min_grams": 0}))

    def test_first_part_is_rounded_to_half_units_and_the_rest_completes_the_dose(self):
        self.assertEqual(split.parts(Decimal("9.5"), 60), (Decimal("5.5"), Decimal("4.0")))
        self.assertEqual(split.parts(Decimal(10), 60), (Decimal(6), Decimal(4)))

    def test_marker_round_trips_through_the_note(self):
        tag = episodes.split_tag("4", 120)
        self.assertEqual(tag, "[DASP:4@120]")
        self.assertEqual(episodes.split_plan("Pasta " + tag), {"units": "4", "delay_minutes": 120})
        self.assertIsNone(episodes.split_plan("Pasta"))

    def test_plan_stays_open_until_a_later_dose_or_three_hours_past_due(self):
        meal_at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        meal = {"at": meal_at, "role": "meal", "carbs": "100", "insulin": "6", "eventType": "Meal Bolus",
                "split": {"units": "4", "delay_minutes": 120}}
        plan = split.open_plan([meal], meal_at + timedelta(minutes=100))
        self.assertEqual((plan["units"], plan["due_at"]), (Decimal(4), meal_at + timedelta(hours=2)))
        self.assertIsNone(split.open_plan([meal], meal_at + timedelta(hours=5, minutes=1)))
        first_part = {"at": meal_at + timedelta(minutes=10), "insulin": "1", "eventType": "Correction Bolus"}
        self.assertIsNotNone(split.open_plan([meal, first_part], meal_at + timedelta(hours=2)))
        second = {"at": meal_at + timedelta(minutes=125), "insulin": "4", "eventType": "Correction Bolus"}
        self.assertIsNone(split.open_plan([meal, second], meal_at + timedelta(minutes=130)))


if __name__ == "__main__":
    unittest.main()
