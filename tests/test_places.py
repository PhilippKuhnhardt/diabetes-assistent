import unittest
from datetime import datetime, timedelta, timezone

from app import places


NOW = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)
CANTEEN = {"id": 1, "place_id": 1, "lat": 49.0, "lon": 8.4}
RESTAURANT_A = {"id": 2, "place_id": 2, "lat": 49.01, "lon": 8.41}
RESTAURANT_B = {"id": 3, "place_id": 2, "lat": 48.99, "lon": 8.38}


def position(lat, lon, accuracy=20, captured=NOW):
    return places.parse_position({"lat": lat, "lon": lon, "accuracy_m": accuracy,
                                  "captured_at": captured.isoformat()})


class PlaceRuleTests(unittest.TestCase):
    def test_distance_is_metres_on_the_globe(self):
        # 0.001° latitude ≈ 111 m.
        self.assertAlmostEqual(places.distance_m(49.0, 8.4, 49.001, 8.4), 111.2, delta=0.5)

    def test_position_inside_a_spot_suggests_its_place_and_any_spot_of_a_place_counts(self):
        spots = [CANTEEN, RESTAURANT_A, RESTAURANT_B]
        self.assertEqual(places.suggestion(position(49.0005, 8.4), NOW, NOW, spots)["place_id"], 1)
        self.assertEqual(places.suggestion(position(48.9902, 8.38), NOW, NOW, spots)["place_id"], 2)
        far = places.suggestion(position(49.2, 8.4), NOW, NOW, spots)
        self.assertEqual((far["status"], far["place_id"]), ("unknown", None))

    def test_overlapping_spots_take_the_nearest(self):
        near = {**RESTAURANT_A, "lat": 49.0012, "lon": 8.4}
        self.assertEqual(places.match(position(49.001, 8.4), [CANTEEN, near])["id"], near["id"])

    def test_position_is_used_only_for_an_entry_timed_now_with_a_precise_fresh_fix(self):
        spots = [CANTEEN]
        self.assertEqual(places.suggestion(position(49.0, 8.4), NOW - timedelta(hours=2), NOW, spots)["status"], "offset")
        # A fix taken while typing still counts when the entry is saved minutes later.
        typed = position(49.0, 8.4, captured=NOW - timedelta(minutes=12))
        self.assertEqual(places.suggestion(typed, NOW, NOW, spots)["status"], "matched")
        stale = position(49.0, 8.4, captured=NOW - timedelta(minutes=45))
        self.assertEqual(places.suggestion(stale, NOW, NOW, spots)["status"], "offset")
        self.assertEqual(places.suggestion(position(49.0, 8.4, accuracy=900), NOW, NOW, spots)["status"], "inaccurate")
        self.assertEqual(places.suggestion(None, NOW, NOW, spots)["status"], "none")

    def test_invalid_positions_are_dropped(self):
        for raw in (None, {}, {"lat": "x", "lon": 1}, {"lat": 95, "lon": 1}, {"lat": float("nan"), "lon": 1}):
            self.assertIsNone(places.parse_position(raw))

    def test_position_in_no_spot_suggests_the_fallback_place(self):
        away = places.suggestion(position(49.2, 8.4), NOW, NOW, [CANTEEN], fallback_id=7)
        self.assertEqual((away["status"], away["place_id"]), ("away", 7))
        self.assertEqual(places.suggestion(position(49.0, 8.4), NOW, NOW, [CANTEEN], fallback_id=7)["place_id"], 1)
        # Only a precise, fresh position says Philipp is elsewhere.
        coarse = places.suggestion(position(49.2, 8.4, accuracy=900), NOW, NOW, [CANTEEN], fallback_id=7)
        self.assertEqual((coarse["status"], coarse["place_id"]), ("inaccurate", None))
        self.assertEqual(places.suggestion(None, NOW, NOW, [CANTEEN], fallback_id=7)["place_id"], None)

    def test_spot_and_name_validation(self):
        self.assertEqual(places.validate_name("  Kantine   Nord "), "Kantine Nord")
        with self.assertRaises(ValueError):
            places.validate_name(" ")
        self.assertEqual(places.validate_spot({"lat": 49, "lon": 8.4}), {"lat": 49, "lon": 8.4})
        self.assertEqual(places.validate_spot({"coordinates": " 49.0069, 8.4037 "}), {"lat": 49.0069, "lon": 8.4037})
        for bad in ({"lat": None, "lon": 8.4}, {"coordinates": "49.0069"}, {"coordinates": "Kantine, Nord"}):
            with self.assertRaises(ValueError):
                places.validate_spot(bad)


if __name__ == "__main__":
    unittest.main()
