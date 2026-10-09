import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

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
        self.assertIn("Bis 11:00", due[0]["message"])
        self.assertIn("17 IE", due[0]["message"])
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
        self.assertIn("4,5 IE zur Mahlzeit um 12:30", due[0]["message"])
        second = {"at": MEAL_AT + timedelta(minutes=121), "insulin": "4", "eventType": "Correction Bolus"}
        self.assertEqual(notifications.due_reminders(MEAL_AT + timedelta(minutes=125), SETTINGS, [MEAL, second],
                                                     [{"units": "17"}], ON), [])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()
        self.env = patch.dict(os.environ, {"PUSHOVER_APP_TOKEN": "t" * 30, "PUSHOVER_USER_KEY": "u" * 30})
        self.env.start()
        self.inputs = patch.object(notifications, "_today_inputs", return_value=([MEAL], []))
        self.inputs.start()
        self.settings = patch.object(notifications.config, "get_settings", return_value=(1, SETTINGS))
        self.settings.start()

    def tearDown(self):
        self.settings.stop()
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
        notifications.set_enabled({"kind": "basal", "enabled": False})
        notifications.set_enabled({"kind": "split", "enabled": False})
        with patch.object(pushover, "send") as send:
            self.assertEqual(notifications.check(MEAL_AT + timedelta(minutes=130)), [])
            send.assert_not_called()
        self.assertEqual(notifications.view()["enabled"], {"basal": False, "split": False})
        with self.assertRaises(ValueError):
            notifications.set_enabled({"kind": "low", "enabled": True})
        with patch.dict(os.environ, {"PUSHOVER_APP_TOKEN": ""}):
            self.assertFalse(notifications.view()["configured"])
            with self.assertRaisesRegex(ValueError, "nicht eingerichtet"):
                notifications.send_test({})


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
                patch.object(background.summary, "history_days", return_value=0), \
                patch.object(background.summary, "summary"), \
                patch.object(background.day_view, "day_payload"), \
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
