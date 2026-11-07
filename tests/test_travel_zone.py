"""Time-zone switches while travelling (user request 2026-11-07: 4 weeks 6 h ahead)."""
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from app import episodes
from app.common import TravelZone, local_day_range, local_zone, zone_name_at
from app.services import settings as settings_service
from app.store import db, timezones

UTC = timezone.utc
AWAY = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)   # 11:00 Berlin = 17:00 Bangkok
BACK = datetime(2026, 12, 6, 2, 0, tzinfo=UTC)    # 09:00 Bangkok = 03:00 Berlin


class TravelZoneTest(unittest.TestCase):
    def setUp(self):
        self.zone = TravelZone("Europe/Berlin", ((AWAY, "Asia/Bangkok"), (BACK, "")))

    def test_local_clock_follows_the_zone_in_effect(self):
        self.assertEqual(datetime(2026, 11, 8, 9, 0, tzinfo=UTC).astimezone(self.zone).hour, 10)
        self.assertEqual(datetime(2026, 11, 10, 1, 0, tzinfo=UTC).astimezone(self.zone).hour, 8)
        self.assertEqual(datetime(2026, 12, 7, 7, 0, tzinfo=UTC).astimezone(self.zone).hour, 8)

    def test_round_trip_also_across_both_switches(self):
        instant = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
        while instant < datetime(2026, 12, 8, tzinfo=UTC):
            self.assertEqual(instant.astimezone(self.zone).astimezone(UTC), instant)
            instant += timedelta(minutes=30)

    def test_day_boundaries(self):
        start = lambda day: datetime.combine(day, time.min, tzinfo=self.zone).astimezone(UTC)
        self.assertEqual(start(date(2026, 11, 8)), datetime(2026, 11, 7, 23, 0, tzinfo=UTC))
        self.assertEqual(start(date(2026, 11, 9)), datetime(2026, 11, 8, 17, 0, tzinfo=UTC))
        self.assertEqual(start(date(2026, 12, 6)), datetime(2026, 12, 5, 17, 0, tzinfo=UTC))
        self.assertEqual(start(date(2026, 12, 7)), datetime(2026, 12, 6, 23, 0, tzinfo=UTC))

    def test_repeated_hour_on_the_way_home(self):
        early = datetime(2026, 12, 5, 20, 0, tzinfo=UTC).astimezone(self.zone)   # 03:00 Bangkok
        late = datetime(2026, 12, 6, 2, 0, tzinfo=UTC).astimezone(self.zone)     # 03:00 Berlin
        self.assertEqual((early.hour, early.fold, late.hour, late.fold), (3, 0, 3, 1))
        self.assertEqual(late.astimezone(UTC), BACK)


class TravelSwitchServiceTest(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()
        self.settings = {"timezone": "Europe/Berlin", "breakfast_end_minute": 660, "lunch_end_minute": 1020}

    def tearDown(self):
        db.DB_PATH = self.old_path
        timezones.forget()
        self.temp.cleanup()

    def test_without_switch_the_home_zone_is_unchanged(self):
        self.assertEqual(str(local_zone("Europe/Berlin")), "Europe/Berlin")
        self.assertEqual(datetime(2026, 11, 9, 7, 0, tzinfo=UTC).astimezone(local_zone("Europe/Berlin")).hour, 8)

    def test_switch_changes_meal_kind_from_then_on_only(self):
        settings_service.set_timezone({"zone": "Asia/Bangkok", "at": AWAY.isoformat()}, now=AWAY + timedelta(hours=2))
        # 07:00 UTC on 2026-11-09 is 14:00 Bangkok: lunch, not breakfast (08:00 Berlin).
        self.assertEqual(episodes.meal_kind_at(datetime(2026, 11, 9, 7, 0, tzinfo=UTC), self.settings)[1], "lunch")
        # Before the switch nothing moves: 07:00 UTC on 2026-11-07 is 08:00 Berlin.
        self.assertEqual(episodes.meal_kind_at(datetime(2026, 11, 7, 7, 0, tzinfo=UTC), self.settings)[1], "breakfast")
        self.assertEqual(local_day_range("2026-11-01", "Europe/Berlin")[0],
                         datetime(2026, 10, 31, 23, tzinfo=UTC) - timedelta(hours=10))
        state = settings_service.travel_state(now=AWAY + timedelta(hours=2))
        self.assertEqual((state["current"], state["current_offset_minutes"], state["home_offset_minutes"]),
                         ("Asia/Bangkok", 420, 60))
        self.assertEqual(zone_name_at("Europe/Berlin", AWAY - timedelta(minutes=1)), "Europe/Berlin")

    def test_back_home_and_undo(self):
        now = AWAY + timedelta(hours=1)
        settings_service.set_timezone({"zone": "Asia/Bangkok"}, now=AWAY)
        with self.assertRaises(ValueError):
            settings_service.set_timezone({"zone": "Asia/Bangkok"}, now=now)
        state = settings_service.set_timezone({"zone": ""}, now=now)
        self.assertEqual(state["current"], "Europe/Berlin")
        self.assertEqual(len(state["switches"]), 2)
        state = settings_service.set_timezone({"undo": True}, now=now)
        self.assertEqual(state["current"], "Asia/Bangkok")

    def test_rejects_unknown_zone_and_old_or_unordered_times(self):
        with self.assertRaises(ValueError):
            settings_service.set_timezone({"zone": "Mars/Olympus"}, now=AWAY)
        with self.assertRaises(ValueError):
            settings_service.set_timezone({"zone": "Asia/Bangkok", "at": (AWAY - timedelta(days=20)).isoformat()},
                                          now=AWAY)
        settings_service.set_timezone({"zone": "Asia/Bangkok"}, now=AWAY)
        with self.assertRaises(ValueError):
            settings_service.set_timezone({"zone": "", "at": (AWAY - timedelta(hours=1)).isoformat()},
                                          now=AWAY + timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
