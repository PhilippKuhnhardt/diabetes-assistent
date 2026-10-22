import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from app import glucose_alerts
from app.ingestion import pushover
from app.services import background, notifications
from app.store import db

SETTINGS = {"timezone": "Europe/Berlin", "breakfast_end_minute": 660, "basal_default_units": "17"}
ON = {"basal": True, "split": True}
# 2026-10-09 is CEST (UTC+2): 09:00 UTC is 11:00 local, the default breakfast end.
MEAL_AT = datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc)
MEAL = {"at": MEAL_AT, "role": "meal", "carbs": "100", "insulin": "6", "eventType": "Meal Bolus",
        "split": {"units": "4.5", "delay_minutes": 120}}


class DueReminderTests(unittest.TestCase):
    def test_basal_reminder_from_breakfast_end_while_the_day_has_no_basal(self):
        before = notifications.due_reminders(datetime(2026, 10, 9, 8, 59, tzinfo=timezone.utc), SETTINGS, [], [], ON)
        self.assertEqual(before, [])
        due = notifications.due_reminders(datetime(2026, 10, 9, 9, tzinfo=timezone.utc), SETTINGS, [], [], ON)
        self.assertEqual([item["key"] for item in due], ["basal:2026-10-09"])
        self.assertEqual("Üblich 17 IE", due[0]["message"])
        dose = {"at": datetime(2026, 10, 9, 6, tzinfo=timezone.utc), "units": "17"}
        self.assertEqual(notifications.due_reminders(datetime(2026, 10, 9, 9, tzinfo=timezone.utc),
                                                     SETTINGS, [], [dose], ON), [])
        self.assertEqual(notifications.due_reminders(datetime(2026, 10, 9, 9, tzinfo=timezone.utc),
                                                     SETTINGS, [], [], {"basal": False, "split": True}), [])

    def test_split_reminder_once_the_second_part_is_due_and_not_injected(self):
        now = MEAL_AT + timedelta(minutes=119)
        self.assertEqual(notifications.due_reminders(now, SETTINGS, [MEAL], [{"units": "17"}], ON), [])
        due = notifications.due_reminders(MEAL_AT + timedelta(minutes=120), SETTINGS, [MEAL], [{"units": "17"}], ON)
        self.assertEqual([item["kind"] for item in due], ["split"])
        self.assertEqual(due[0]["key"], "split:2026-10-09T10:30:00+00:00")
        self.assertEqual("4,5 IE zur Mahlzeit um 12:30", due[0]["message"])
        second = {"at": MEAL_AT + timedelta(minutes=121), "insulin": "4", "eventType": "Correction Bolus"}
        self.assertEqual(notifications.due_reminders(MEAL_AT + timedelta(minutes=125), SETTINGS, [MEAL, second],
                                                     [{"units": "17"}], ON), [])

    def test_split_reminder_follows_the_glucose_course_and_names_the_forecast_amount(self):
        cgm = lambda minutes, values: [{"at": MEAL_AT + timedelta(minutes=minutes - 15 + 5 * step), "sgv": value}
                                       for step, value in enumerate(values)]
        due = lambda minutes, values, units=None: notifications.due_reminders(
            MEAL_AT + timedelta(minutes=minutes), SETTINGS, [MEAL], [{"units": "17"}], ON, cgm(minutes, values),
            (lambda: units))
        # 75 min after the meal: high and rising fast means the second part now.
        early = due(75, [170, 180, 190, 200], Decimal("5.5"))
        self.assertEqual([item["title"] for item in early], [notifications.EARLY_SPLIT_TITLE])
        self.assertEqual(early[0]["message"], "5,5 IE laut Prognose (geplant 4,5) zur Mahlzeit um 12:30")
        self.assertEqual(due(75, [180, 182, 184, 186]), [])
        self.assertEqual(due(50, [170, 180, 190, 200]), [])
        # At the planned time: low or falling waits up to an hour, then it is due anyway.
        self.assertEqual(due(120, [104, 104, 105, 105]), [])
        self.assertEqual(due(130, [150, 145, 140, 135]), [])
        late = due(180, [104, 104, 105, 105], Decimal(0))
        self.assertEqual(late[0]["message"], "Laut Prognose nicht nötig (geplant 4,5) zur Mahlzeit um 12:30")
        self.assertEqual(len(due(120, [140, 141, 142, 143], Decimal(3))), 1)

    def test_glucose_warning_shows_only_the_numbers(self):
        now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        found = [{"kind": "low", "at": now + timedelta(minutes=25), "glucose_mgdl": 95, "mgdl": 62},
                 {"kind": "high", "at": now + timedelta(minutes=40), "glucose_mgdl": 210, "mgdl": 268}]
        with patch.object(notifications.marks, "analysis_day_types", return_value={}), \
                patch.object(notifications.marks, "vacation_periods", return_value=[]), \
                patch.object(notifications.marks, "day_type_for", return_value=("work",)), \
                patch.object(notifications.marks, "sport_session_effects", return_value={}), \
                patch.object(notifications.factors, "forecast_settings", return_value=(None, None, 30)), \
                patch.object(notifications.factors, "meal_points", return_value=[]), \
                patch.object(notifications.factors, "carb_factor", return_value=None), \
                patch.object(notifications.on_board, "sources", return_value=[]), \
                patch.object(notifications.glucose_alerts, "warnings", return_value=found):
            due = notifications.glucose_warnings(now, SETTINGS, [], [], {"low": True, "high": True})
        self.assertEqual([item["message"] for item in due],
                         ["BZ 95, unter 70 um 14:25 (in 25 min), Tiefstwert 62",
                          "BZ 210, über 250 um 14:40 (in 40 min), Höchstwert 268"])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()
        self.env = patch.dict(os.environ, {"PUSHOVER_APP_TOKEN": "t" * 30, "PUSHOVER_USER_KEY": "u" * 30})
        self.env.start()
        self.inputs = patch.object(notifications, "_today_inputs", return_value=([MEAL], [], []))
        self.inputs.start()
        self.warnings = patch.object(notifications, "glucose_warnings", return_value=[])
        self.warnings.start()
        self.settings = patch.object(notifications.config, "get_settings", return_value=(1, SETTINGS))
        self.settings.start()
        self.second = patch.object(notifications, "_second_part", return_value=None)
        self.second.start()

    def tearDown(self):
        self.second.stop()
        self.settings.stop()
        self.warnings.stop()
        self.inputs.stop()
        self.env.stop()
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_each_reminder_is_sent_once_and_a_failed_send_is_retried(self):
        now = MEAL_AT + timedelta(minutes=130)
        with patch.object(pushover, "send", side_effect=pushover.PushoverError("weg")):
            with self.assertRaises(pushover.PushoverError):
                notifications.check(now)
        with patch.object(pushover, "send") as send:
            sent = notifications.check(now)
            self.assertEqual([item["kind"] for item in sent], ["basal", "split"])
            self.assertEqual(send.call_count, 2)
            self.assertEqual(notifications.check(now + timedelta(minutes=5)), [])
            self.assertEqual(send.call_count, 2)
        view = notifications.view()
        self.assertTrue(view["configured"])
        self.assertEqual([row["kind"] for row in view["recent"]], ["basal", "split"])

    def test_switched_off_kinds_and_missing_setup_send_nothing(self):
        for kind in notifications.KINDS:
            notifications.set_enabled({"kind": kind, "enabled": False})
        with patch.object(pushover, "send") as send:
            self.assertEqual(notifications.check(MEAL_AT + timedelta(minutes=130)), [])
            send.assert_not_called()
        self.assertEqual(notifications.view()["enabled"], {"basal": False, "split": False, "low": False, "high": False})
        with self.assertRaises(ValueError):
            notifications.set_enabled({"kind": "other", "enabled": True})
        with patch.dict(os.environ, {"PUSHOVER_APP_TOKEN": ""}):
            self.assertFalse(notifications.view()["configured"])
            with self.assertRaisesRegex(ValueError, "nicht eingerichtet"):
                notifications.send_test({})


    def test_priority_per_kind_is_stored_and_used(self):
        self.assertEqual(notifications.view()["priorities"], {"basal": 1, "split": 1, "low": 1, "high": 1})
        notifications.set_enabled({"kind": "split", "priority": 2})
        notifications.set_enabled({"kind": "basal", "priority": -1})
        view = notifications.view()
        self.assertEqual(view["priorities"], {"basal": -1, "split": 2, "low": 1, "high": 1})
        self.assertTrue(view["enabled"]["split"])
        self.assertEqual([option["value"] for option in view["priority_options"]], [-2, -1, 0, 1, 2])
        for wrong in (3, "1", True, None):
            with self.assertRaises(ValueError):
                notifications.set_enabled({"kind": "low", "priority": wrong})
        with patch.object(pushover, "send") as send:
            notifications.check(MEAL_AT + timedelta(minutes=130))
        self.assertEqual([call.args[2] for call in send.call_args_list], [-1, 2])

    def test_a_glucose_warning_repeats_at_most_once_per_hour(self):
        notifications.set_enabled({"kind": "basal", "enabled": False})
        notifications.set_enabled({"kind": "split", "enabled": False})
        warn = lambda now, *args: [{"key": "low:" + now.isoformat(), "kind": "low", "title": "Tief droht", "message": "x"}]
        self.warnings.stop()
        self.warnings = patch.object(notifications, "glucose_warnings", side_effect=warn)
        self.warnings.start()
        with patch.object(pushover, "send") as send:
            for minutes in (0, 5, 55, 61):
                notifications.check(MEAL_AT + timedelta(minutes=minutes))
        self.assertEqual(send.call_count, 2)


class GlucoseAlertTests(unittest.TestCase):
    NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)  # 14:00 in Berlin

    def entries(self, values):
        """Readings every 5 minutes ending at NOW, oldest first."""
        return [{"at": self.NOW - timedelta(minutes=5 * (len(values) - 1 - index)), "sgv": value}
                for index, value in enumerate(values)]

    def found(self, doses=(), carbs=()):
        return {"doses": [{"at": at, "units": Decimal(units), "mgdl_per_unit": Decimal(35)} for at, units in doses],
                "carbs": [{"at": at, "grams": Decimal(grams), "fat": False, "mgdl_per_gram": Decimal("3.5")}
                          for at, grams in carbs],
                "surcharges": [], "fresh_meals": []}

    def kinds(self, entries, found, now=None):
        return [item["kind"] for item in glucose_alerts.warnings(entries, found, now or self.NOW, "Europe/Berlin")]

    def test_active_insulin_without_carbs_warns_of_a_low(self):
        found = self.found(doses=[(self.NOW - timedelta(minutes=20), "4")])
        self.assertEqual(self.kinds(self.entries([110] * 7), found), ["low"])
        warning = glucose_alerts.warnings(self.entries([110] * 7), found, self.NOW, "Europe/Berlin")[0]
        self.assertGreater(warning["at"], self.NOW)
        self.assertLess(warning["mgdl"], 70)
        self.assertEqual(self.kinds(self.entries([110] * 7), self.found()), [])

    def test_falling_faster_than_the_model_warns_of_a_low(self):
        self.assertEqual(self.kinds(self.entries([130, 124, 118, 112, 106, 100, 94]), self.found()), ["low"])
        self.assertEqual(self.kinds(self.entries([130, 124, 118, 112, 106, 100, 94]),
                                    self.found(carbs=[(self.NOW - timedelta(minutes=40), "20")])), [])

    def test_no_low_warning_right_after_carbs(self):
        falling = self.entries([140, 132, 124, 116, 108, 100, 92])
        just_eaten = self.found(carbs=[(self.NOW - timedelta(minutes=10), "20")])
        self.assertEqual(self.kinds(falling, just_eaten), [])
        # A small carb correction eaten because BG falls does not pause the warning.
        self.assertEqual(self.kinds(falling, self.found(carbs=[(self.NOW - timedelta(minutes=10), "5")])), ["low"])

    def test_carbs_still_to_come_warn_of_a_high(self):
        found = self.found(carbs=[(self.NOW - timedelta(minutes=10), "60")])
        self.assertEqual(self.kinds(self.entries([200] * 7), found), ["high"])
        self.assertEqual(self.kinds(self.entries([260] * 7), found), [])

    def test_no_warning_at_night_or_without_a_fresh_reading(self):
        found = self.found(doses=[(self.NOW - timedelta(minutes=20), "4")])
        for hours, expected in ((8, ["low"]), (9, []), (10, []), (15, []), (16, ["low"])):  # 22:00, 23:00, 0:00, 5:00, 6:00 local
            moment = self.NOW + timedelta(hours=hours)
            shifted = [{**row, "at": row["at"] + timedelta(hours=hours)} for row in self.entries([110] * 7)]
            self.assertEqual(self.kinds(shifted, self.found(doses=[(moment - timedelta(minutes=20), "4")]), moment),
                             expected, hours)
        self.assertEqual(self.kinds(self.entries([110] * 7), found, self.NOW + timedelta(minutes=11)), [])


class PushoverTests(unittest.TestCase):
    def test_send_posts_the_message_and_reports_rejections_without_secrets(self):
        env = {"PUSHOVER_APP_TOKEN": "token-secret", "PUSHOVER_USER_KEY": "user-secret",
               "APP_PUBLIC_URL": "https://aps.example.org"}
        with patch.dict(os.environ, env), patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"status":1}'
            pushover.send("Titel", "Text")
            body = urlopen.call_args.args[0].data.decode()
            self.assertIn("priority=1", body)
            self.assertIn("url=https%3A%2F%2Faps.example.org", body)
            self.assertNotIn("retry=", body)
            pushover.send("Titel", "Text", 2)
            body = urlopen.call_args.args[0].data.decode()
            self.assertIn("priority=2", body)
            self.assertIn("retry=60", body)
            self.assertIn("expire=1800", body)
            error = urllib.error.HTTPError(pushover.API_URL, 400, "Bad", {},
                                           io.BytesIO(json.dumps({"errors": ["user key is invalid"]}).encode()))
            urlopen.side_effect = error
            with self.assertRaises(pushover.PushoverError) as raised:
                pushover.send("Titel", "Text")
            self.assertIn("user key is invalid", str(raised.exception))
            self.assertNotIn("secret", str(raised.exception))


class BackgroundTests(unittest.TestCase):
    def test_reminders_wait_for_a_successful_sync_of_today(self):
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        status = {"sync_error": None, "basal_sync_error": None}
        with patch.object(background.nightscout, "is_configured", return_value=True), \
                patch.object(background.config, "get_settings", return_value=(1, SETTINGS)), \
                patch.object(background, "_precompute", return_value=False), \
                patch.object(background.pushover, "heartbeat") as heartbeat, \
                patch.object(background.notifications, "check", return_value=[]) as check:
            with patch.object(background.sync, "sync_day",
                              side_effect=lambda day: {**status, "sync_error": "kaputt"} if day == today.isoformat()
                              else status):
                background.run_once()
            check.assert_not_called()
            heartbeat.assert_not_called()
            with patch.object(background.sync, "sync_day", return_value=status):
                background.run_once()
            check.assert_called_once()
            heartbeat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
