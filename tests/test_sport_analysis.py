import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import sport_analysis

LABELS = {"jogging": "Joggen", "racing": "Rennsport"}


def meal(meal_id, at, delta, insulin="80", carb_effect="1600", kind="lunch", **extra):
    return {
        "id": meal_id, "day": at.date().isoformat(), "meal": kind, "at": at.isoformat(),
        "excluded": False, "sport": extra.pop("sport", {"status": "auto"}),
        "calculation": {"start": at.isoformat(), "end": (at + timedelta(minutes=180)).isoformat(),
                        "delta_mgdl": delta, "insulin_effect_mgdl": insulin,
                        "carbs_times_isf_total": carb_effect, "carbs_total": "40",
                        "rapid_units_in_window": "2", "blockers": [], **extra},
    }


class SportAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.day = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)

    def analyze(self, meals, exercises):
        return sport_analysis.analyze(meals, exercises, 360, 180, LABELS)

    def test_sport_effect_is_difference_to_meal_type_baseline(self):
        # Baseline meals: 10 × (ΔG + 80) / 1600 → 0.6 and 0.8 IE/KE, median 0.7.
        meals = [
            meal("a", self.day - timedelta(days=2), "16"),
            meal("b", self.day - timedelta(days=1), "48"),
            meal("c", self.day, "-8"),
        ]
        exercises = [{"id": "s1", "at": (self.day - timedelta(minutes=90)).isoformat(),
                      "duration_minutes": 45, "kind": "jogging", "notes": ""}]
        result = self.analyze(meals, exercises)
        self.assertEqual(Decimal(result["baselines"]["lunch"]["median_units_per_ke"]), Decimal("0.7"))
        session = result["sessions"][0]
        self.assertEqual(session["label"], "Joggen")
        self.assertEqual(session["end"], (self.day - timedelta(minutes=45)).isoformat())
        influenced = session["meals"][0]
        # Expected ΔG = 0.7 × 1600 / 10 − 80 = 32; observed −8 → −40 mg/dL.
        self.assertEqual(Decimal(influenced["expected_delta_mgdl"]), Decimal(32))
        self.assertEqual(Decimal(influenced["effect_mgdl"]), Decimal(-40))
        self.assertEqual(Decimal(result["kinds"][0]["median_effect_mgdl"]), Decimal(-40))

    def test_racing_raises_glucose_and_unknown_kind_is_labelled(self):
        meals = [meal("a", self.day - timedelta(days=1), "32"), meal("c", self.day, "62")]
        exercises = [
            {"id": "r1", "at": (self.day + timedelta(minutes=30)).isoformat(),
             "duration_minutes": 60, "kind": "racing", "notes": "Training"},
            {"id": "x", "at": (self.day - timedelta(days=5)).isoformat(),
             "duration_minutes": 30, "kind": None, "notes": ""},
        ]
        result = self.analyze(meals, exercises)
        racing = next(item for item in result["sessions"] if item["id"] == "r1")
        self.assertEqual(Decimal(racing["meals"][0]["effect_mgdl"]), Decimal(30))
        unknown = next(item for item in result["sessions"] if item["id"] == "x")
        self.assertEqual(unknown["label"], "Sportart unbekannt")
        self.assertEqual(unknown["meals"], [])

    def test_confirmed_sport_correction_does_not_change_raw_factor(self):
        meals = [meal("a", self.day - timedelta(days=1), "32"),
                 meal("c", self.day, "-8", sport={"status": "affected", "effect_mgdl": "40"},
                      sport_effect_mgdl="40")]
        result = self.analyze(meals, [])
        session = result["sessions"][0]
        self.assertEqual(session["label"], "Sport ohne Eintrag")
        self.assertEqual(Decimal(session["meals"][0]["effect_mgdl"]), Decimal(-40))
        self.assertEqual(session["meals"][0]["confirmed_effect_mgdl"], "40")

    def test_missing_baseline_and_blocked_meals_have_reason(self):
        meals = [meal("c", self.day, "-8"),
                 meal("d", self.day + timedelta(hours=6), "0", kind="dinner",
                      blockers=[{"kind": "meal"}], reason="Weitere Hauptmahlzeit")]
        exercises = [{"id": "s1", "at": (self.day - timedelta(minutes=60)).isoformat(),
                      "duration_minutes": 30, "kind": "jogging", "notes": ""},
                     {"id": "s2", "at": (self.day + timedelta(hours=5)).isoformat(),
                      "duration_minutes": 30, "kind": "jogging", "notes": ""}]
        result = self.analyze(meals, exercises)
        by_id = {item["id"]: item for item in result["sessions"]}
        self.assertEqual(by_id["s1"]["meals"][0]["reason"], "Keine Vergleichsmahlzeiten ohne Sport")
        self.assertEqual(by_id["s2"]["meals"][-1]["reason"], "Weitere Hauptmahlzeit")
        self.assertIsNone(result["kinds"][0]["median_effect_mgdl"])


if __name__ == "__main__":
    unittest.main()
