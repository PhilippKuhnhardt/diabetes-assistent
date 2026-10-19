import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import dosing, episodes, forecast
from app.common import unit_text
from app.analysis.night import night_analysis
from app.services import day_view
from app.store import config, db, marks, mirror
from app.server import Handler


MEAL_AT = "2026-09-23T10:00:00Z"


@contextlib.contextmanager
def fake_days(**day_payload):
    """Patch the stored diary days with fakes. Stored analysis pages are keyed by day
    fingerprints, so the fakes get their own fingerprint (still following local marks)."""
    token = uuid.uuid4().hex
    real = day_view.day_fingerprint
    with patch("app.services.day_view.day_payload", **day_payload) as mock, \
            patch("app.services.day_view.day_fingerprint", side_effect=lambda day: token + real(day)):
        yield mock


def then_empty(payloads):
    """day_payload stand-in: the given days first, then empty days."""
    remaining = iter(payloads)
    return lambda *args, **kwargs: next(remaining, {"meals": []})


class FakeNightscout(BaseHTTPRequestHandler):
    treatments = []
    profile = {
        "_id": "current-profile",
        "startDate": "2026-01-01T00:00:00.000Z",
        "defaultProfile": "Default",
        "store": {"Default": {
            "timezone": "Europe/Berlin", "units": "mg/dl",
            "sens": [
                {"time": "00:00", "timeAsSeconds": 0, "value": 50},
                {"time": "12:00", "timeAsSeconds": 43200, "value": 45},
            ],
            "carbratio": [
                {"time": "00:00", "timeAsSeconds": 0, "value": 10},
                {"time": "12:00", "timeAsSeconds": 43200, "value": 20},
            ],
        }},
    }
    entries = [
        {"_id": "glucose-start", "date": 1790157600000, "type": "sgv", "sgv": 100},
        {"_id": "glucose-end", "date": 1790168400000, "type": "sgv", "sgv": 120},
    ]

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        assert urllib.parse.parse_qs(parsed.query)["token"] == ["test-raw-token"]
        if parsed.path.endswith("/entries/sgv.json"):
            value = self.entries
        elif parsed.path.endswith("/treatments.json"):
            value = self.treatments
        elif parsed.path.endswith("/profile/current"):
            value = self.profile
        else:
            self.send_error(404)
            return
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        assert urllib.parse.parse_qs(parsed.query)["token"] == ["test-raw-token"]
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for item in value:
            item["_id"] = "ns-" + str(len(self.treatments) + 1)
            self.treatments.append(item)
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        assert urllib.parse.parse_qs(parsed.query)["token"] == ["test-raw-token"]
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if parsed.path.rstrip("/") == "/api/v1/treatments":
            record_id = value.get("_id")
            for index, existing in enumerate(self.treatments):
                if existing.get("_id") == record_id:
                    self.treatments[index] = value
                    break
            else:
                self.send_error(404)
                return
            raw = json.dumps(value).encode()
        elif parsed.path.rstrip("/") == "/api/v1/profile":
            self.__class__.profile = value
            raw = json.dumps(self.profile).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        assert urllib.parse.parse_qs(parsed.query)["token"] == ["test-raw-token"]
        record_id = parsed.path.rsplit("/", 1)[-1]
        before = len(self.treatments)
        self.treatments[:] = [item for item in self.treatments if item.get("_id") != record_id]
        if len(self.treatments) == before:
            self.send_error(404)
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


INITIAL_PROFILE = json.loads(json.dumps(FakeNightscout.profile))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_env = dict(os.environ)
        self.old_db = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()
        FakeNightscout.treatments = []
        FakeNightscout.profile = json.loads(json.dumps(INITIAL_PROFILE))
        FakeNightscout.entries = [
            {"_id": "glucose-start", "date": 1790157600000, "type": "sgv", "sgv": 100},
            {"_id": "glucose-end", "date": 1790168400000, "type": "sgv", "sgv": 120},
            {"_id": "glucose-extended", "date": int(datetime(2026, 9, 23, 15, 30, tzinfo=timezone.utc).timestamp() * 1000), "type": "sgv", "sgv": 120},
            {"_id": "glucose-snack-end", "date": int(datetime(2026, 9, 23, 14, tzinfo=timezone.utc).timestamp() * 1000), "type": "sgv", "sgv": 120},
        ]
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeNightscout)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        os.environ.update({
            "NIGHTSCOUT_URL": f"http://127.0.0.1:{self.fake.server_port}",
            "NIGHTSCOUT_AUTH_MODE": "token",
            "NIGHTSCOUT_CREDENTIAL": "test-raw-token",
            "APP_PASSWORD": "",
        })
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.app.server_port}"
        self.request("/api/settings", method="POST", body={
            "isf_schedule": [{"start_minute": 0, "mgdl_per_unit": 50}],
            "peak_minutes": 60, "duration_minutes": 180, "window_minutes": 180,
        })

    def tearDown(self):
        self.app.shutdown()
        self.fake.shutdown()
        self.app.server_close()
        self.fake.server_close()
        db.DB_PATH = self.old_db
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None):
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        req = urllib.request.Request(self.base + path, data=raw, headers=headers, method=method)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())

    def test_current_glucose_endpoint_uses_live_nightscout_values(self):
        now = datetime.now(timezone.utc)
        FakeNightscout.profile["store"]["Default"]["target_low"] = [{"time": "00:00", "value": 100}]
        FakeNightscout.profile["store"]["Default"]["target_high"] = [{"time": "00:00", "value": 160}]
        self.request("/api/profile/sync", method="POST", body={})
        FakeNightscout.entries = [
            {"_id": "prior", "date": int((now - timedelta(minutes=12)).timestamp() * 1000),
             "type": "sgv", "sgv": 120},
            {"_id": "latest", "date": int((now - timedelta(minutes=2)).timestamp() * 1000),
             "type": "sgv", "sgv": 130},
        ]
        result = self.request("/api/glucose/current")
        self.assertEqual(result["status"], "in_range")
        self.assertEqual(Decimal(result["glucose_mgdl"]), Decimal(130))
        self.assertEqual(Decimal(result["trend_delta_mgdl"]), Decimal(15))
        self.assertEqual(result["trend_direction"], "up")
        self.assertTrue(result["fresh"])

    def test_local_dose_target_is_saved_without_changing_nightscout_profile(self):
        initial = self.request("/api/settings")
        self.assertEqual(Decimal(str(initial["settings"]["dose_target_mgdl"])), Decimal(100))
        profile_before = json.loads(json.dumps(FakeNightscout.profile))
        updated = self.request("/api/settings", method="POST", body={"dose_target_mgdl": "115"})
        self.assertEqual(Decimal(updated["settings"]["dose_target_mgdl"]), Decimal(115))
        self.assertEqual(FakeNightscout.profile, profile_before)
        self.assertEqual(Decimal(self.request("/api/settings")["settings"]["dose_target_mgdl"]), Decimal(115))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/settings", method="POST", body={"dose_target_mgdl": "NaN"})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as high:
            self.request("/api/settings", method="POST", body={"dose_target_mgdl": "141"})
        self.assertEqual(high.exception.code, 400)
        high.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as above:
            self.request("/api/settings", method="POST", body={"dose_low_target_mgdl": "116"})
        self.assertEqual(above.exception.code, 400)
        above.exception.close()
        lower = self.request("/api/settings", method="POST", body={"dose_low_target_mgdl": "95"})
        self.assertEqual(Decimal(lower["settings"]["dose_low_target_mgdl"]), Decimal(95))

    def test_dose_help_suggestion_can_be_written_once_to_nightscout(self):
        self.request("/api/settings")  # Sync the current Nightscout I:C and KF schedules.
        now = datetime.now(timezone.utc)
        current_day = {
            "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 150}],
            "treatments": [],
        }
        with fake_days(return_value=current_day):
            suggestion = self.request("/api/dose-help", method="POST", body={"carbs": "20"})
        self.assertEqual(Decimal(suggestion["target_mgdl"]), Decimal(100))
        self.assertEqual(Decimal(suggestion["cautious_target_mgdl"]), Decimal(140))
        self.assertGreater(Decimal(suggestion["entry_units"]),
                           Decimal(suggestion["cautious_entry_units"]))
        event = {
            "kind": "meal", "at": now.isoformat(), "carbs": suggestion["carbs_g"],
            "insulin": suggestion["entry_units"],
            "event_id": "c19ff4d0-6aa7-4f2e-a17b-73966761c29e",
        }
        self.request("/api/treatment", method="POST", body=event)
        self.request("/api/treatment", method="POST", body=event)
        self.assertEqual(len(FakeNightscout.treatments), 1)
        self.assertEqual(Decimal(str(FakeNightscout.treatments[0]["insulin"])),
                         Decimal(suggestion["entry_units"]))
        self.assertEqual(Decimal(str(FakeNightscout.treatments[0]["carbs"])), Decimal(20))

    def test_diary_additions_join_one_meal_and_keep_full_carbs_and_doses(self):
        FakeNightscout.entries = [
            {"_id": "meal-start", "date": int(datetime(2026, 9, 23, 9, 17, tzinfo=timezone.utc).timestamp() * 1000),
             "type": "sgv", "sgv": 100},
            {"_id": "meal-end", "date": int(datetime(2026, 9, 23, 13, 38, tzinfo=timezone.utc).timestamp() * 1000),
             "type": "sgv", "sgv": 120},
        ]
        records = [
            ("2026-09-23T09:17:00Z", "10", "0"),
            ("2026-09-23T10:38:00Z", "95", "6"),
            ("2026-09-23T12:03:00Z", "5", "0"),
            ("2026-09-23T12:36:00Z", "17", "0"),
        ]
        for at, carbs, insulin in records:
            self.request("/api/treatment", method="POST", body={
                "kind": "meal", "at": at, "carbs": carbs, "insulin": insulin,
            })
        self.assertEqual([item["eventType"] for item in FakeNightscout.treatments],
                         ["Meal Bolus", "Snack Bolus", "Carb Correction", "Carb Correction"])
        day = self.request("/api/day?date=2026-09-23")
        self.assertEqual(len(day["meals"]), 1)
        self.assertEqual([item["kind"] for item in day["timeline"]],
                         ["meal", "snack", "snack", "snack"])
        self.assertEqual([item["id"] for item in day["meals"][0]["events"]],
                         ["ns-1", "ns-2", "ns-3", "ns-4"])
        self.assertEqual(Decimal(day["meals"][0]["calculation"]["carbs_total"]), Decimal(127))
        self.assertEqual(Decimal(day["meals"][0]["calculation"]["rapid_units_in_window"]), Decimal(6))

    def test_existing_diary_meal_boluses_are_grouped_without_rewriting_nightscout(self):
        FakeNightscout.treatments = [
            {"_id": "root", "created_at": MEAL_AT, "eventType": "Meal Bolus",
             "carbs": 50, "insulin": 4, "notes": "[DAE:00000000-0000-0000-0000-000000000001]"},
            {"_id": "more", "created_at": "2026-09-23T11:00:00Z", "eventType": "Meal Bolus",
             "carbs": 10, "insulin": 1, "notes": "[DAE:00000000-0000-0000-0000-000000000002]"},
        ]
        day = self.request("/api/day?date=2026-09-23")
        self.assertEqual(len(day["meals"]), 1)
        self.assertEqual([item["kind"] for item in day["timeline"]], ["meal", "snack"])
        self.assertEqual(Decimal(day["meals"][0]["calculation"]["carbs_total"]), Decimal(60))
        self.assertEqual(FakeNightscout.treatments[1]["eventType"], "Meal Bolus")

    def test_diary_entry_within_two_hours_is_snack_across_meal_kinds(self):
        # 09:33 breakfast, 11:07 after breakfast end (11:00), 12:05 > 2 h later.
        FakeNightscout.treatments = [
            {"_id": "root", "created_at": "2026-09-23T07:33:00Z", "eventType": "Meal Bolus",
             "carbs": 77, "insulin": 8.5, "notes": "[DAE:00000000-0000-0000-0000-000000000001]"},
            {"_id": "late", "created_at": "2026-09-23T09:07:00Z", "eventType": "Meal Bolus",
             "carbs": 10, "insulin": 0, "notes": "[DAE:00000000-0000-0000-0000-000000000002]"},
            {"_id": "lunch", "created_at": "2026-09-23T10:05:00Z", "eventType": "Meal Bolus",
             "carbs": 40, "insulin": 3, "notes": "[DAE:00000000-0000-0000-0000-000000000003]"},
        ]
        day = self.request("/api/day?date=2026-09-23")
        self.assertEqual([item["kind"] for item in day["timeline"]], ["meal", "snack", "meal"])
        self.assertEqual([item["meal"] for item in day["meals"]], ["breakfast", "lunch"])
        self.assertEqual(day["timeline"][1]["notes"], "")

    def test_new_entry_can_be_kept_as_main_meal(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": "2026-09-23T07:33:00Z", "carbs": "50", "insulin": "4",
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": "2026-09-23T09:07:00Z", "carbs": "30", "insulin": "2", "main_meal": True,
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": "2026-09-23T09:20:00Z", "carbs": "10", "insulin": "0",
        })
        self.assertEqual([item["eventType"] for item in FakeNightscout.treatments],
                         ["Meal Bolus", "Meal Bolus", "Carb Correction"])
        day = self.request("/api/day?date=2026-09-23")
        self.assertEqual([item["kind"] for item in day["timeline"]], ["meal", "meal", "snack"])
        self.assertEqual(len(day["meals"]), 2)

    def test_meal_without_insulin_and_insulin_without_carbs_are_separate_entries(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "30", "insulin": 0,
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "correction", "at": "2026-09-23T10:30:00Z", "carbs": 0, "insulin": "1.5",
        })
        day = self.request("/api/day?date=2026-09-23")
        self.assertEqual([item["kind"] for item in day["timeline"]], ["meal", "correction"])
        self.assertEqual(Decimal(str(day["timeline"][0]["insulin"])), Decimal(0))
        self.assertEqual(Decimal(str(day["timeline"][1]["carbs"])), Decimal(0))
        self.assertEqual(len(day["meals"]), 1)
        self.assertEqual(Decimal(str(day["meals"][0]["carbs"])), Decimal(30))

    def test_standalone_carb_and_correction_entries_can_be_edited(self):
        at = "2026-09-23T10:30:00Z"
        self.request("/api/treatment", method="POST", body={
            "kind": "carbs", "at": at, "carbs": "16", "notes": "Saft",
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "correction", "at": "2026-09-23T11:00:00Z", "insulin": "1.5",
        })
        day = self.request("/api/day?date=2026-09-23")
        carb = next(item for item in day["timeline"] if item["kind"] == "carbs")
        self.assertEqual(Decimal(str(carb["carbs"])), Decimal(16))
        self.assertTrue(any(item["kind"] == "correction" for item in day["timeline"]))
        self.request("/api/treatment", method="POST", body={
            "id": carb["id"], "original_day": "2026-09-23", "kind": "snack",
            "at": at, "carbs": "20", "insulin": "1", "notes": "Apfel",
        })
        updated = self.request("/api/day?date=2026-09-23")
        snack = next(item for item in updated["timeline"] if item["id"] == carb["id"])
        self.assertEqual(snack["kind"], "snack")
        self.assertEqual(Decimal(str(snack["carbs"])), Decimal(20))
        self.assertEqual(Decimal(str(snack["insulin"])), Decimal(1))
        self.assertEqual(snack["notes"], "Apfel")

    def test_existing_carbs_change_from_13_to_130_is_visible(self):
        at = "2026-09-23T09:53:00Z"
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": at, "carbs": "13", "insulin": "1",
        })
        before = self.request("/api/day?date=2026-09-23")
        entry = next(item for item in before["timeline"] if Decimal(str(item["carbs"])) == 13)
        self.request("/api/treatment", method="POST", body={
            "id": entry["id"], "original_day": "2026-09-23", "kind": "meal",
            "at": at, "carbs": "130", "insulin": "1",
        })
        after = self.request("/api/day?date=2026-09-23")
        saved = next(item for item in after["timeline"] if item["id"] == entry["id"])
        self.assertEqual(Decimal(str(saved["carbs"])), Decimal(130))
        self.assertEqual(len(FakeNightscout.treatments), 1)

    def test_tagged_meal_can_be_updated_without_losing_its_episode(self):
        at = "2026-09-23T09:53:00Z"
        episode_id = "cb529c35-e44b-4fe5-a69b-d2aec69ae7fe"
        self.request("/api/episode", method="POST", body={
            "at": at, "meal": "lunch", "carbs": "13", "insulin": "1",
            "episode_id": episode_id,
        })
        before = self.request("/api/day?date=2026-09-23")
        entry = next(item for item in before["timeline"] if Decimal(str(item["carbs"])) == 13)
        self.request("/api/treatment", method="POST", body={
            "id": entry["id"], "original_day": "2026-09-23", "kind": "meal",
            "at": at, "carbs": "130", "insulin": "1",
        })
        after = self.request("/api/day?date=2026-09-23")
        saved = next(item for item in after["timeline"] if item["id"] == entry["id"])
        self.assertEqual(Decimal(str(saved["carbs"])), Decimal(130))
        self.assertEqual(after["meals"][0]["id"], episode_id)
        self.assertEqual(len(FakeNightscout.treatments), 1)

    def test_sport_effect_is_confirmed_per_meal_and_does_not_exclude_it(self):
        meal_at = "2026-09-23T10:00:00Z"
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": meal_at, "carbs": "40", "insulin": "4",
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "exercise", "at": "2026-09-23T08:00:00Z",
            "sport_kind": "gym", "duration_minutes": 60,
        })
        before = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertTrue(before["sport"]["suggested"])
        self.assertEqual(Decimal(before["sport"]["suggested_effect_mgdl"]), Decimal(40))
        self.assertEqual(Decimal(before["calculation"]["sport_effect_mgdl"]), Decimal(0))
        self.request("/api/analysis/sport", method="POST", body={
            "meal_id": before["id"], "day": "2026-09-23", "status": "affected",
            "effect_mgdl": "35",
        })
        after = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertEqual(Decimal(after["calculation"]["sport_effect_mgdl"]), Decimal(35))
        self.assertEqual(
            Decimal(after["calculation"]["adjusted_delta_mgdl"]),
            Decimal(after["calculation"]["delta_mgdl"]) + 35,
        )
        self.assertEqual(marks.analysis_exclusions(), set())

    def test_sport_inside_meal_window_is_suggested_for_that_meal_and_the_next(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": "2026-09-23T07:00:00Z", "carbs": "40", "insulin": "4",
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "exercise", "at": "2026-09-23T08:20:00Z",
            "sport_kind": "gym", "duration_minutes": 60,
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": "2026-09-23T11:00:00Z", "carbs": "40", "insulin": "4",
        })
        meals = self.request("/api/day?date=2026-09-23")["meals"]
        self.assertEqual([meal["meal"] for meal in meals], ["breakfast", "lunch"])
        for meal in meals:
            self.assertTrue(meal["sport"]["suggested"], meal["meal"])
            self.assertEqual(Decimal(meal["sport"]["suggested_effect_mgdl"]), Decimal(40))

    def test_sport_after_the_meal_window_is_not_suggested(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": "2026-09-23T07:00:00Z", "carbs": "40", "insulin": "4",
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "exercise", "at": "2026-09-23T10:10:00Z",
            "sport_kind": "gym", "duration_minutes": 30,
        })
        meal = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertFalse(meal["sport"]["suggested"])

    def test_racing_raises_glucose_and_is_subtracted_from_delta(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": "2026-09-23T10:00:00Z", "carbs": "40", "insulin": "4",
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "exercise", "at": "2026-09-23T08:00:00Z",
            "sport_kind": "racing", "duration_minutes": 90,
        })
        self.assertIn("[DAS1:racing]", FakeNightscout.treatments[-1]["notes"])
        day = self.request("/api/day?date=2026-09-23")
        racing = next(item for item in day["timeline"] if item["kind"] == "exercise")
        self.assertEqual(racing["sport_kind"], "racing")
        meal = day["meals"][0]
        self.assertEqual(Decimal(meal["sport"]["suggested_effect_mgdl"]), Decimal(-40))
        self.request("/api/analysis/sport", method="POST", body={
            "meal_id": meal["id"], "day": "2026-09-23", "status": "affected",
            "effect_mgdl": "-30",
        })
        after = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertEqual(Decimal(after["calculation"]["sport_effect_mgdl"]), Decimal(-30))
        self.assertEqual(
            Decimal(after["calculation"]["adjusted_delta_mgdl"]),
            Decimal(after["calculation"]["delta_mgdl"]) - 30,
        )

    def test_settings_without_racing_get_its_default_and_stay_editable(self):
        _, current = config.get_settings()
        legacy = {**current, "sport_effects_mgdl": {"jogging": 40, "gym": 40, "light_cardio": 20}}
        config.save_settings(legacy)
        effects = self.request("/api/settings")["settings"]["sport_effects_mgdl"]
        self.assertEqual(Decimal(effects["racing"]), Decimal(-40))
        self.request("/api/settings", method="POST", body={
            "sport_effects_mgdl": {**effects, "racing": "-60"},
        })
        effects = self.request("/api/settings")["settings"]["sport_effects_mgdl"]
        self.assertEqual(Decimal(effects["racing"]), Decimal(-60))

    def test_former_lookbacks_map_to_the_offered_ones(self):
        _, current = config.get_settings()
        for old, new in ((14, 15), (21, 30), (28, 30)):
            config.save_settings({**current, "auto_lookback_days": old})
            self.assertEqual(self.request("/api/settings")["settings"]["auto_lookback_days"], new)
        self.request("/api/settings", method="POST", body={"auto_lookback_days": 8})
        self.assertEqual(self.request("/api/settings")["settings"]["auto_lookback_days"], 8)

    def test_manual_meal_window_is_persisted_validated_and_resettable(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4",
        })
        before = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertFalse(before["calculation"]["manual_window"])
        self.assertEqual(before["calculation"]["end"], "2026-09-23T13:00:00+00:00")
        for start, end in (("2026-09-23T12:05:00Z", "2026-09-23T14:00:00Z"),
                           ("2026-09-23T10:40:00Z", "2026-09-23T11:00:00Z"),
                           ("2026-09-23T07:00:00Z", "2026-09-23T14:00:00Z"),
                           ("2026-09-23T10:00:00Z", "2026-09-23T10:10:00Z")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/analysis/window", method="POST", body={
                    "meal_id": before["id"], "day": "2026-09-23", "start": start, "end": end,
                })
            self.assertEqual(error.exception.code, 400)
        self.request("/api/analysis/window", method="POST", body={
            "meal_id": before["id"], "day": "2026-09-23",
            "start": MEAL_AT, "end": "2026-09-23T14:00:00Z",
        })
        manual = self.request("/api/day?date=2026-09-23")["meals"][0]["calculation"]
        self.assertTrue(manual["manual_window"])
        self.assertEqual(manual["end"], "2026-09-23T14:00:00+00:00")
        self.assertEqual(manual["auto_end"], "2026-09-23T13:00:00+00:00")
        self.assertEqual(manual["end_reason"], "manual")
        summary = self.request("/api/summary?days=30")
        self.assertTrue(any(item["manual_window"] for group in summary["groups"].values()
                            for item in group["calculations"]))
        self.request("/api/analysis/window", method="POST", body={
            "meal_id": before["id"], "day": "2026-09-23", "reset": True,
        })
        reset = self.request("/api/day?date=2026-09-23")["meals"][0]["calculation"]
        self.assertFalse(reset["manual_window"])
        self.assertEqual(reset["end"], "2026-09-23T13:00:00+00:00")
        self.assertEqual(marks.analysis_windows(), {})

    def test_calculated_day_is_stored_and_recalculated_when_an_input_changes(self):
        from app.services import day_view
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4",
        })
        first = self.request("/api/day?date=2026-09-23")
        with patch("app.services.day_view.compute_day", wraps=day_view.compute_day) as compute:
            self.assertEqual(day_view.day_payload("2026-09-23", refresh=False)["meals"], first["meals"])
            self.assertEqual(compute.call_count, 0)
            meal_id = first["meals"][0]["id"]
            changes = [
                ("/api/analysis/sport", {"meal_id": meal_id, "day": "2026-09-23", "status": "affected",
                                         "effect_mgdl": "30"}),
                ("/api/analysis/window", {"meal_id": meal_id, "day": "2026-09-23",
                                          "start": MEAL_AT, "end": "2026-09-23T14:00:00Z"}),
                ("/api/analysis/day-type", {"day": "2026-09-23", "day_type": "free"}),
                ("/api/settings", {"window_minutes": 170}),
                ("/api/basal", {"at": "2026-09-23T06:00:00Z", "units": "17"}),
            ]
            for path, body in changes:
                with self.subTest(path):
                    calls = compute.call_count
                    self.request(path, method="POST", body=body)
                    day_view.day_payload("2026-09-23", refresh=False)
                    self.assertEqual(compute.call_count, calls + 1)
                    day_view.day_payload("2026-09-23", refresh=False)
                    self.assertEqual(compute.call_count, calls + 1)
            # A fresh Nightscout read without new data keeps the stored day; a new reading does not.
            self.request("/api/day?date=2026-09-23")  # brings the basal note just sent to Nightscout
            calls = compute.call_count
            self.request("/api/day?date=2026-09-23")
            self.assertEqual(compute.call_count, calls)
            FakeNightscout.entries.append({"_id": "glucose-new", "date": int(datetime(
                2026, 9, 23, 16, tzinfo=timezone.utc).timestamp() * 1000), "type": "sgv", "sgv": 130})
            self.request("/api/day?date=2026-09-23")
            self.assertEqual(compute.call_count, calls + 1)
            with patch("app.services.day_view.CODE_VERSION", "other code"):
                day_view.day_payload("2026-09-23", refresh=False)
            self.assertEqual(compute.call_count, calls + 2)
        stored = day_view.day_payload("2026-09-23", refresh=False)
        self.assertEqual(stored["meals"][0]["sport"]["effect_mgdl"], "30")
        self.assertTrue(stored["meals"][0]["calculation"]["manual_window"])
        self.assertEqual(stored["day_type"], "free")
        self.assertEqual(len(stored["basal"]), 1)

    def test_analysis_page_is_stored_until_a_day_or_an_exclusion_changes(self):
        from app.services import summary
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4",
        })
        meal_id = self.request("/api/day?date=2026-09-23")["meals"][0]["id"]
        with patch("app.services.summary.compute_summary", wraps=summary.compute_summary) as compute:
            first = self.request("/api/summary?days=30")
            self.assertEqual(self.request("/api/summary?days=30"), first)
            self.assertEqual(compute.call_count, 1)
            self.request("/api/summary?days=14")
            self.assertEqual(compute.call_count, 2)
            self.request("/api/analysis/exclusion", method="POST",
                         body={"meal_id": meal_id, "day": "2026-09-23", "excluded": True})
            excluded = self.request("/api/summary?days=30")
            self.assertEqual(compute.call_count, 3)
            self.request("/api/day?date=2026-09-23")  # fresh Nightscout read, nothing new
            self.request("/api/summary?days=30")
            self.assertEqual(compute.call_count, 3)
            FakeNightscout.entries.append({"_id": "glucose-new", "date": int(datetime(
                2026, 9, 23, 16, tzinfo=timezone.utc).timestamp() * 1000), "type": "sgv", "sgv": 130})
            self.request("/api/day?date=2026-09-23")
            self.request("/api/summary?days=30")
            self.assertEqual(compute.call_count, 4)
        self.assertNotEqual(excluded, first)

    def test_background_round_mirrors_today_and_stores_days_and_analysis_pages(self):
        from app.services import background, summary
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        self.assertIsNone(mirror.synced_at(today.isoformat()))
        background._last_inputs = None
        self.assertEqual(background.run_once(), {"errors": {}, "computed": True})
        for day in (today, today - timedelta(days=1)):
            self.assertIsNotNone(mirror.synced_at(day.isoformat()))
        with db.connect() as connection:
            days = connection.execute("SELECT COUNT(*) FROM day_results").fetchone()[0]
            pages = {row[0] for row in connection.execute("SELECT key FROM summary_results")}
        self.assertEqual(days, summary.history_days(30) + 1)
        self.assertEqual(pages, {"1", "3", "7", "14", "30"})
        # Stored results answer the next request without calculating again.
        with patch("app.services.summary.compute_summary") as compute, \
                patch("app.services.day_view.compute_day") as compute_day:
            self.request("/api/summary?days=14")
            compute.assert_not_called()
            compute_day.assert_not_called()
        # Without new data a round only syncs and checks reminders; a new reading precomputes again.
        with patch("app.services.background.summary.summary") as page:
            self.assertEqual(background.run_once(), {"errors": {}, "computed": False})
            page.assert_not_called()
            FakeNightscout.entries.append({"_id": "glucose-today", "type": "sgv", "sgv": 110,
                                           "date": int(datetime.now(timezone.utc).timestamp() * 1000) - 60000})
            self.assertEqual(background.run_once(), {"errors": {}, "computed": True})
            self.assertEqual(page.call_count, 5)
        with patch.dict(os.environ, {"APP_SYNC_MINUTES": "0"}):
            self.assertIsNone(background.start())
        with patch.dict(os.environ, {"APP_SYNC_MINUTES": "soon"}):
            with self.assertRaises(SystemExit):
                background.interval_minutes()

    def test_blocked_calculation_still_exposes_editable_window(self):
        FakeNightscout.entries = FakeNightscout.entries[:1] + [
            {"_id": "glucose-1405", "date": int(datetime(2026, 9, 23, 14, 5, tzinfo=timezone.utc).timestamp() * 1000),
             "type": "sgv", "sgv": 130},
            {"_id": "glucose-1355", "date": int(datetime(2026, 9, 23, 13, 55, tzinfo=timezone.utc).timestamp() * 1000),
             "type": "sgv", "sgv": 130},
        ]
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4",
        })
        meal = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertIn("CGM", meal["calculation"]["reason"])
        self.assertEqual(meal["calculation"]["window_end"], "2026-09-23T13:00:00+00:00")
        self.assertFalse(meal["calculation"]["manual_window"])
        self.request("/api/analysis/window", method="POST", body={
            "meal_id": meal["id"], "day": "2026-09-23",
            "start": MEAL_AT, "end": "2026-09-23T14:00:00Z",
        })
        fixed = self.request("/api/day?date=2026-09-23")["meals"][0]["calculation"]
        self.assertIsNotNone(fixed["factor_units_per_ke"])
        self.assertEqual(Decimal(fixed["g3"]), Decimal(130))

    def test_surcharge_method_is_a_setting(self):
        self.assertEqual(self.request("/api/settings")["settings"]["auto_method"], "percentile")
        saved = self.request("/api/settings", method="POST", body={"auto_method": "surcharge"})
        self.assertEqual(saved["settings"]["auto_method"], "surcharge")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/settings", method="POST", body={"auto_method": "band"})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        summary = self.request("/api/summary?days=1")
        self.assertEqual(summary["suggestion_derivation"]["method"], "surcharge")
        self.assertTrue(summary["groups"]["dinner"]["suggestion"]["surcharge"])

    def test_surcharge_amount_is_a_setting(self):
        self.assertEqual(self.request("/api/settings")["settings"]["auto_surcharge_mgdl"], "40")
        saved = self.request("/api/settings", method="POST", body={"auto_method": "surcharge", "auto_surcharge_mgdl": 60})
        self.assertEqual(saved["settings"]["auto_surcharge_mgdl"], "60")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/settings", method="POST", body={"auto_surcharge_mgdl": 0})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        derivation = self.request("/api/summary?days=1")["suggestion_derivation"]
        self.assertEqual((derivation["surcharge_mgdl"], derivation["active_surcharge_mgdl"]), ("60", "60"))
        self.assertEqual([row["key"] for row in derivation["tuning"]], ["percentile", "surcharge", "half_life", "lookback"])
        surcharge = next(row for row in derivation["tuning"] if row["key"] == "surcharge")
        self.assertEqual(surcharge["options"][0]["apply"], {"auto_method": "percentile"})
        self.assertEqual(surcharge["options"][1]["apply"], {"auto_method": "surcharge", "auto_surcharge_mgdl": "20"})

    def test_surcharge_goes_to_main_meals_not_snacks(self):
        self.request("/api/settings")  # Sync the current Nightscout I:C and KF schedules.
        self.request("/api/settings", method="POST", body={"auto_method": "surcharge"})
        now = datetime.now(timezone.utc)
        current_day = {"entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 150}], "treatments": []}
        with fake_days(return_value=current_day), \
                patch("app.forecast.predict_method", return_value={"count": 5, "units_per_ke": Decimal(1)}):
            meal = self.request("/api/dose-help", method="POST", body={"carbs": "20"})
            snack = self.request("/api/dose-help", method="POST", body={"carbs": "20", "snack": True})
        # 20 g is half of the full-surcharge size: 20 mg/dL at the current KF (50 or 45 by clock).
        expected = Decimal(20) / Decimal(meal["isf_mgdl_per_unit"])
        self.assertEqual(Decimal(meal["auto_surcharge_units"]), expected)
        self.assertNotIn("auto_surcharge_units", snack)
        self.assertAlmostEqual(Decimal(meal["auto_carb_units"]) - Decimal(snack["auto_carb_units"]), expected, places=20)

    def test_quality_marks_are_stored_in_nightscout_and_shown_per_meal(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4", "notes": "Pizza",
            "carbs_certainty": "known", "fat_protein": True, "alcohol": True,
        })
        notes = FakeNightscout.treatments[0]["notes"]
        self.assertIn("[DAQ:known]", notes)
        self.assertIn("[DAQ:fat]", notes)
        self.assertIn("[DAQ:alcohol]", notes)
        day = self.request("/api/day?date=2026-09-23")
        self.assertEqual(day["meals"][0]["quality"], {"carbs_certainty": "known", "fat_protein": True, "alcohol": True})
        entry = next(item for item in day["timeline"] if item["kind"] == "meal")
        self.assertEqual(entry["notes"], "Pizza")
        self.assertEqual(entry["carbs_certainty"], "known")
        self.assertTrue(entry["alcohol"])
        self.request("/api/treatment", method="POST", body={
            "id": entry["id"], "original_day": "2026-09-23", "kind": "meal", "at": MEAL_AT,
            "carbs": "40", "insulin": "4", "notes": "Pizza", "carbs_certainty": None, "fat_protein": True,
        })
        self.assertNotIn("[DAQ:known]", FakeNightscout.treatments[0]["notes"])
        meal = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertEqual(meal["quality"], {"carbs_certainty": None, "fat_protein": True, "alcohol": False})
        row = self.request("/api/v1/meals?from=2026-09-23&to=2026-09-23")["rows"][0]
        self.assertEqual((row["carbs_certainty"], row["fat_protein"], row["alcohol"]), (None, True, False))

    def test_meal_takes_least_certain_carb_level_and_old_unsure_marks_still_count(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4", "carbs_certainty": "weighed",
        })
        # An older client still sends carbs_unsure; existing [DAQ:unsure] notes keep meaning "unsure".
        later = (datetime.fromisoformat(MEAL_AT) + timedelta(minutes=30)).isoformat()
        self.request("/api/treatment", method="POST", body={
            "kind": "carbs", "at": later, "carbs": "10", "insulin": "0", "carbs_unsure": True,
        })
        self.assertIn("[DAQ:unsure]", FakeNightscout.treatments[-1]["notes"])
        meal = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertEqual(meal["quality"]["carbs_certainty"], "unsure")
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/treatment", method="POST", body={
                "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4", "carbs_certainty": "maybe"})

    def test_read_api_needs_the_key_and_returns_table_rows_as_json(self):
        self.request("/api/treatment", method="POST", body={"kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4"})
        meal = self.request("/api/day?date=2026-09-23")["meals"][0]
        key = "k" * 40
        os.environ["APP_PASSWORD"] = "secret"

        def get(path, token=None, method="GET"):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            data = b"{}" if method == "POST" else None
            if data:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(self.base + path, headers=headers, method=method, data=data)
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read())

        def status(path, token=None, method="GET"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                get(path, token, method)
            error.exception.close()
            return error.exception.code

        # Without API_READ_KEY no key opens the API.
        self.assertEqual(status("/api/v1/meals", key), 401)
        os.environ["API_READ_KEY"] = "short"
        self.assertEqual(status("/api/v1/meals", "short"), 401)
        os.environ["API_READ_KEY"] = key
        self.assertEqual(status("/api/v1/meals"), 401)
        self.assertEqual(status("/api/v1/meals", "k" * 39), 401)
        # The key only opens the read API, never the app or writes.
        self.assertEqual(status("/api/settings", key), 401)
        self.assertEqual(status("/api/day?date=2026-09-23", key), 401)
        self.assertEqual(status("/api/settings", key, method="POST"), 401)
        index = get("/api/v1", key)
        self.assertIn("/api/v1/meals", index["endpoints"])
        self.assertEqual(index["first_day"], "2026-09-23")
        meals = get("/api/v1/meals?from=2026-09-22&to=2026-09-24", key)
        self.assertEqual((meals["from"], meals["to"]), ("2026-09-22", "2026-09-24"))
        self.assertEqual([(row["meal_id"], row["day"], row["excluded"]) for row in meals["rows"]],
                         [(meal["id"], "2026-09-23", False)])
        self.assertEqual(meals["rows"][0]["factor_ie_per_ke"], meal["calculation"]["factor_units_per_ke"])
        data = get("/api/v1/data?from=2026-09-23&to=2026-09-23", key)
        self.assertEqual(sorted(data["tables"]), ["basal", "entries", "meals", "treatments", "window_events"])
        self.assertIn(100, [row["sgv"] for row in data["tables"]["entries"]])
        self.assertEqual([row["eventType"] for row in data["tables"]["treatments"]], ["Meal Bolus"])
        # Neighbouring day payloads overlap; a multi-day range still lists every row once.
        wide = get("/api/v1/data?from=2026-09-22&to=2026-09-24", key)["tables"]
        self.assertEqual([row["eventType"] for row in wide["treatments"]], ["Meal Bolus"])
        stamps = [row["at"] for row in wide["entries"]]
        self.assertEqual(len(stamps), len(set(stamps)))
        self.assertEqual(len(stamps), len(data["tables"]["entries"]) + sum(
            len(get(f"/api/v1/entries?from={day}&to={day}", key)["rows"]) for day in ("2026-09-22", "2026-09-24")))
        self.assertTrue(get("/api/v1/profile", key)["settings"]["isf_schedule"])
        self.assertEqual(status("/api/v1/unknown", key), 404)
        self.assertEqual(status("/api/v1/meals?days=0", key), 400)
        self.assertEqual(status("/api/v1/meals?from=gestern", key), 400)
        os.environ["API_READ_KEY"] = "n" * 40
        self.assertEqual(status("/api/v1/meals", key), 401)

    def test_basal_is_stored_locally_mirrored_as_note_and_ignored_by_meal_factor(self):
        self.request("/api/treatment", method="POST", body={"kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4"})
        before = self.request("/api/day?date=2026-09-23")["meals"][0]["calculation"]
        dose_id = "6f1c2f55-3c55-4a3f-9d0e-5d3b0c1d2e3f"
        saved = self.request("/api/basal", method="POST", body={
            "at": "2026-09-23T10:05:00Z", "units": "17", "event_id": dose_id})
        self.assertEqual((saved["id"], saved["day"], saved["nightscout_synced"]), (dose_id, "2026-09-23", True))
        # Retrying the same entry does not duplicate it in Nightscout.
        self.request("/api/basal", method="POST", body={"at": "2026-09-23T10:05:00Z", "units": "17", "event_id": dose_id})
        note = [item for item in FakeNightscout.treatments if item["eventType"] == "Note"]
        self.assertEqual(len(note), 1)
        self.assertNotIn("insulin", note[0])
        self.assertEqual(note[0]["basal_units"], 17)
        self.assertIn("[DA2:basal]", note[0]["notes"])
        day = self.request("/api/day?date=2026-09-23")
        after = day["meals"][0]["calculation"]
        self.assertEqual(after["factor_units_per_ke"], before["factor_units_per_ke"])
        self.assertEqual(after["rapid_units_in_window"], before["rapid_units_in_window"])
        self.assertEqual([(item["id"], item["units"], item["nightscout_synced"]) for item in day["basal"]],
                         [(dose_id, "17", True)])
        basal_rows = [item for item in day["timeline"] if item["kind"] == "basal"]
        self.assertEqual([(item["id"], item["basal_units"]) for item in basal_rows], [(dose_id, "17")])
        self.assertEqual(len([item for item in day["timeline"] if item["eventType"] == "Note"]), 0)
        self.assertEqual(day["other_events"], [])
        rows = self.request("/api/v1/basal?from=2026-09-23&to=2026-09-23")["rows"]
        self.assertEqual([(row["id"], row["units"], row["nightscout_synced"]) for row in rows], [(dose_id, "17", True)])
        self.request("/api/basal/delete", method="POST", body={"id": dose_id})
        self.assertEqual([item["eventType"] for item in FakeNightscout.treatments], ["Meal Bolus"])
        self.assertEqual(self.request("/api/day?date=2026-09-23")["basal"], [])
        for units in ("0", "101", "17.3", "NaN"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/basal", method="POST", body={"at": "2026-09-23T10:05:00Z", "units": units})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

    def test_basal_without_nightscout_is_kept_locally_and_sent_later(self):
        url = os.environ["NIGHTSCOUT_URL"]
        os.environ["NIGHTSCOUT_URL"] = "http://127.0.0.1:1"
        saved = self.request("/api/basal", method="POST", body={"at": "2026-09-23T07:00:00Z", "units": "17.5"})
        self.assertFalse(saved["nightscout_synced"])
        self.assertTrue(saved["nightscout_error"])
        offline = self.request("/api/day?date=2026-09-23")
        self.assertEqual([item["nightscout_synced"] for item in offline["basal"]], [False])
        self.assertEqual(FakeNightscout.treatments, [])
        os.environ["NIGHTSCOUT_URL"] = url
        self.request("/api/day?date=2026-09-23")
        self.assertEqual([item["basal_units"] for item in FakeNightscout.treatments], [17.5])
        online = self.request("/api/day?date=2026-09-23")
        self.assertEqual([item["nightscout_synced"] for item in online["basal"]], [True])
        self.assertEqual(len(FakeNightscout.treatments), 1)
        # Deleted in Nightscout: a synced dose disappears locally too.
        FakeNightscout.treatments = []
        self.assertEqual(self.request("/api/day?date=2026-09-23")["basal"], [])

    def test_basal_note_known_only_to_nightscout_is_adopted(self):
        dose_id = "0b9a7e7c-1d2e-4f3a-8b5c-6d7e8f9a0b1c"
        FakeNightscout.treatments = [{"_id": "ns-basal", "created_at": "2026-09-23T06:30:00Z", "eventType": "Note",
                                      "basal_units": 16, "notes": f"Basal 16 IE [DA2:basal] [DAE:{dose_id}]"}]
        day = self.request("/api/day?date=2026-09-23")
        self.assertEqual([(item["id"], item["units"], item["nightscout_synced"]) for item in day["basal"]],
                         [(dose_id, "16", True)])

    def test_basal_default_is_configurable(self):
        self.assertEqual(self.request("/api/settings")["settings"]["basal_default_units"], "17")
        self.request("/api/settings", method="POST", body={"basal_default_units": "18.5"})
        self.assertEqual(self.request("/api/settings")["settings"]["basal_default_units"], "18.5")
        self.request("/api/settings", method="POST", body={"basal_default_units": "20"})
        self.assertEqual(self.request("/api/settings")["settings"]["basal_default_units"], "20")
        self.request("/api/settings", method="POST", body={"basal_default_units": "0"})
        self.assertEqual(self.request("/api/settings")["settings"]["basal_default_units"], "0")
        for invalid in ("-1", "100.5", "17.2"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/settings", method="POST", body={"basal_default_units": invalid})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

    def test_current_nightscout_profile_is_the_only_isf_source(self):
        data = self.request("/api/settings")
        self.assertEqual(data["settings"]["isf_schedule"], [
            {"start_minute": 0, "mgdl_per_unit": "50"},
            {"start_minute": 720, "mgdl_per_unit": "45"},
        ])
        self.assertEqual(data["settings"]["nightscout_profile"]["profile_name"], "Default")
        self.assertIsNotNone(data["profile_sync_at"])

        self.request("/api/settings", method="POST", body={
            "window_minutes": 180,
            "isf_schedule": [{"start_minute": 0, "mgdl_per_unit": 999}],
        })
        _, settings = config.get_settings()
        self.assertEqual(settings["isf_schedule"][0]["mgdl_per_unit"], "50")

        refreshed = self.request("/api/profile/sync", method="POST", body={})
        self.assertEqual(refreshed["profile"]["profile_name"], "Default")

        original = FakeNightscout.profile
        FakeNightscout.profile = json.loads(json.dumps(original))
        FakeNightscout.profile["store"]["Default"]["units"] = "mmol/L"
        FakeNightscout.profile["store"]["Default"]["sens"][0]["value"] = 2.8
        try:
            self.request("/api/profile/sync", method="POST", body={})
            _, settings = config.get_settings()
            self.assertEqual(settings["isf_schedule"][0]["mgdl_per_unit"], "50.45096")
        finally:
            FakeNightscout.profile = original

    def test_ic_and_sensitivity_changes_are_saved_to_nightscout(self):
        original = json.loads(json.dumps(FakeNightscout.profile))
        profile_id = original["_id"]
        try:
            ic = self.request("/api/profile/ic", method="POST", body={
                "profile_id": profile_id,
                "ic_schedule": [
                    {"start_minute": 0, "grams_per_unit": "12"},
                    {"start_minute": 720, "grams_per_unit": "18"},
                ],
            })
            self.assertEqual(Decimal(ic["settings"]["ic_schedule"][0]["grams_per_unit"]), Decimal("12"))
            self.assertEqual(FakeNightscout.profile["store"]["Default"]["carbratio"][1]["value"], 18)
            self.assertEqual(FakeNightscout.profile["store"]["Default"]["sens"], original["store"]["Default"]["sens"])

            isf = self.request("/api/profile/isf", method="POST", body={
                "profile_id": profile_id,
                "isf_schedule": [
                    {"start_minute": 0, "mgdl_per_unit": "48"},
                    {"start_minute": 720, "mgdl_per_unit": "42"},
                ],
            })
            self.assertEqual(Decimal(isf["settings"]["isf_schedule"][1]["mgdl_per_unit"]), Decimal("42"))
            self.assertEqual(FakeNightscout.profile["store"]["Default"]["carbratio"][0]["value"], 12)
            self.assertEqual(FakeNightscout.profile["store"]["Default"]["timezone"], "Europe/Berlin")
        finally:
            FakeNightscout.profile = original

    def test_ie_per_ke_input_is_converted_to_nightscout_grams_per_unit(self):
        result = self.request("/api/profile/ic", method="POST", body={
            "profile_id": FakeNightscout.profile["_id"],
            "ic_schedule": [
                {"start_minute": 0, "units_per_ke": "0.8"},
                {"start_minute": 720, "units_per_ke": "1.25"},
            ],
        })
        values = FakeNightscout.profile["store"]["Default"]["carbratio"]
        self.assertEqual([row["value"] for row in values], [12.5, 8.0])
        self.assertEqual(result["settings"]["ic_schedule"][0]["grams_per_unit"], "12.5")

    def test_analysis_accepts_one_day(self):
        with fake_days(return_value={"meals": []}):
            summary = self.request("/api/summary?days=1")
        self.assertEqual(summary["days"], 1)
        self.assertEqual(summary["sport_analysis"]["sessions"], [])

    def test_expected_change_uses_current_time_specific_nightscout_profile(self):
        meal = {"id": "lunch-with-snack", "meal": "lunch", "at": "2026-09-23T10:00:00+02:00",
                "calculation": {
                    "factor_units_per_ke": "1", "ratio_g_per_unit": "10",
                    "delta_mgdl": "40", "insulin_effect_mgdl": "45",
                    "carb_events": [
                        {"at": "2026-09-23T10:00:00+02:00", "carbs": "10", "isf": "50"},
                        {"at": "2026-09-23T13:00:00+02:00", "carbs": "10", "isf": "45"},
                    ],
                }}
        with fake_days(side_effect=then_empty([{"meals": [meal]}])):
            row = self.request("/api/summary?days=7")["groups"]["lunch"]["calculations"][0]
        self.assertEqual(Decimal(row["expected_delta_mgdl"]), Decimal("27.5"))
        self.assertEqual(Decimal(row["deviation_mgdl"]), Decimal("12.5"))

    def test_deviation_uses_ie_per_ke_conversion_and_hides_blocked_meals(self):
        FakeNightscout.profile["store"]["Default"]["sens"] = [
            {"time": "00:00", "timeAsSeconds": 0, "value": 30}]
        FakeNightscout.profile["store"]["Default"]["carbratio"] = [
            {"time": "00:00", "timeAsSeconds": 0, "value": 12.5}]
        self.request("/api/profile/sync", method="POST", body={})
        at = "2026-09-23T19:12:00+02:00"
        calculated = {"id": "dinner", "meal": "dinner", "at": at,
                      "calculation": {"factor_units_per_ke": "0.8", "ratio_g_per_unit": "12.5",
                                      "delta_mgdl": "36.8",
                                      "insulin_effect_mgdl": "135",
                                      "carb_events": [{"at": at, "carbs": "55", "isf": "30"}]}}
        blocked = {"id": "blocked", "meal": "dinner", "at": at,
                   "calculation": {**calculated["calculation"], "factor_units_per_ke": None,
                                   "reason": "CGM-Lücke"}}
        with fake_days(side_effect=then_empty([{"meals": [calculated, blocked]}])):
            rows = self.request("/api/summary?days=7")["groups"]["dinner"]["calculations"]
        self.assertEqual(Decimal(rows[0]["expected_delta_mgdl"]), Decimal("-3"))
        self.assertEqual(Decimal(rows[0]["deviation_mgdl"]), Decimal("39.8"))
        self.assertIsNone(rows[1]["deviation_mgdl"])

    def test_sensitivity_write_converts_to_nightscout_mmol_units(self):
        original = FakeNightscout.profile
        FakeNightscout.profile = json.loads(json.dumps(original))
        FakeNightscout.profile["store"]["Default"]["units"] = "mmol/L"
        try:
            result = self.request("/api/profile/isf", method="POST", body={
                "profile_id": original["_id"],
                "isf_schedule": [
                    {"start_minute": 0, "mgdl_per_unit": "54.0546"},
                    {"start_minute": 720, "mgdl_per_unit": "36.0364"},
                ],
            })
            self.assertEqual(FakeNightscout.profile["store"]["Default"]["sens"][0]["value"], 3)
            self.assertEqual(Decimal(result["settings"]["isf_schedule"][1]["mgdl_per_unit"]), Decimal("36.0364"))
        finally:
            FakeNightscout.profile = original

    def test_nightscout_write_read_mirror_and_calculation(self):
        episode_id = "cb529c35-e44b-4fe5-a69b-d2aec69ae7fe"
        event_id = "d2fc91b4-dde4-41c2-bf57-75638224e389"
        created = self.request("/api/episode", method="POST", body={
            "at": MEAL_AT, "meal": "lunch", "carbs": "60", "insulin": "4",
            "episode_id": episode_id, "event_id": event_id,
        })
        self.assertEqual(created["episode_id"], episode_id)
        self.request("/api/episode", method="POST", body={
            "at": MEAL_AT, "meal": "lunch", "carbs": "60", "insulin": "4",
            "episode_id": episode_id, "event_id": event_id,
        })
        self.assertEqual(len(FakeNightscout.treatments), 1)

        before = self.request("/api/day?date=2026-09-23")
        self.assertEqual(len(before["meals"]), 1)
        self.assertIsNotNone(before["meals"][0]["calculation"]["ratio_g_per_unit"])

        self.request("/api/dose", method="POST", body={
            "at": "2026-09-23T12:30:00Z", "meal": "lunch", "role": "correction",
            "insulin": "1", "episode_id": episode_id,
            "event_id": "079a06b1-7b2c-4d99-9171-79014c175b47",
        })
        after = self.request("/api/day?date=2026-09-23")
        result = after["meals"][0]["calculation"]
        self.assertEqual(len(result["doses"]), 2)
        self.assertAlmostEqual(float(result["factor_units_per_ke"]) * float(result["ratio_g_per_unit"]), 10.0)
        self.assertLess(float(result["ratio_g_per_unit"]), float(before["meals"][0]["calculation"]["ratio_g_per_unit"]))
        self.assertIsNotNone(mirror.synced_at("2026-09-23"))
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM treatments").fetchone()[0], 2)

    def test_snack_is_shown_with_meal_when_counted_in_factor(self):
        FakeNightscout.treatments = [
            {"_id": "meal", "created_at": MEAL_AT, "eventType": "Meal Bolus", "carbs": 60, "insulin": 4},
            {"_id": "snack", "created_at": "2026-09-23T11:00:00Z", "eventType": "Snack Bolus", "carbs": 15, "insulin": 1,
             "notes": "[DA1:cb529c35-e44b-4fe5-a69b-d2aec69ae7fe:lunch:correction]"},
            {"_id": "later", "created_at": "2026-09-23T14:00:00Z", "eventType": "Snack Bolus", "carbs": 8, "insulin": 0},
        ]
        day = self.request("/api/day?date=2026-09-23")
        meal = day["meals"][0]
        self.assertEqual(float(meal["calculation"]["carbs_total"]), 75)
        self.assertEqual(float(meal["calculation"]["rapid_units_in_window"]), 5)
        self.assertEqual([event["id"] for event in meal["events"]], ["meal", "snack"])
        self.assertEqual(meal["events"][1]["role"], "correction")
        self.assertEqual(meal["events"][1]["eventType"], "Snack Bolus")
        self.assertEqual(meal["calculation"]["doses"][1]["event_type"], "Snack Bolus")
        self.assertEqual([event["at"] for event in day["other_events"]], ["2026-09-23T14:00:00+00:00"])

    def test_snack_bolus_write_includes_carbs_and_insulin_in_meal(self):
        episode_id = "cb529c35-e44b-4fe5-a69b-d2aec69ae7fe"
        self.request("/api/episode", method="POST", body={
            "at": MEAL_AT, "meal": "lunch", "carbs": "60", "insulin": "4", "episode_id": episode_id,
        })
        self.request("/api/dose", method="POST", body={
            "at": "2026-09-23T11:00:00Z", "meal": "lunch", "role": "snack",
            "carbs": "15", "insulin": "1", "episode_id": episode_id,
        })
        snack = FakeNightscout.treatments[1]
        self.assertEqual(snack["eventType"], "Snack Bolus")
        self.assertEqual(snack["carbs"], 15)
        self.assertEqual(snack["insulin"], 1)
        day = self.request("/api/day?date=2026-09-23")
        meal = day["meals"][0]
        self.assertEqual(len(day["meals"]), 1)
        self.assertEqual(meal["events"][1]["role"], "snack")
        self.assertEqual(float(meal["calculation"]["carbs_total"]), 75)
        self.assertEqual(float(meal["calculation"]["rapid_units_in_window"]), 5)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/dose", method="POST", body={
                "at": "2026-09-23T11:30:00Z", "meal": "lunch", "role": "snack",
                "carbs": "0", "insulin": "1", "episode_id": episode_id,
            })
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(len(FakeNightscout.treatments), 2)

    def test_delete_individual_dose_from_nightscout_and_cache(self):
        episode_id = "cb529c35-e44b-4fe5-a69b-d2aec69ae7fe"
        self.request("/api/episode", method="POST", body={
            "at": MEAL_AT, "meal": "lunch", "carbs": "60", "insulin": "4", "episode_id": episode_id,
        })
        self.request("/api/dose", method="POST", body={
            "at": "2026-09-23T12:30:00Z", "meal": "lunch", "role": "meal-dose",
            "insulin": "1", "episode_id": episode_id,
        })
        before = self.request("/api/day?date=2026-09-23")
        dose_id = before["meals"][0]["events"][1]["id"]
        self.request("/api/treatment/delete", method="POST", body={"id": dose_id, "day": "2026-09-23"})
        self.assertEqual(len(FakeNightscout.treatments), 1)
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM treatments WHERE id=?", (dose_id,)).fetchone()[0], 0)
        after = self.request("/api/day?date=2026-09-23")
        self.assertEqual(len(after["meals"]), 1)
        self.assertEqual([event["id"] for event in after["meals"][0]["events"]], ["ns-1"])
        self.assertEqual(len(after["meals"][0]["calculation"]["doses"]), 1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/treatment/delete", method="POST", body={"id": dose_id, "day": "2026-09-23"})
        self.assertEqual(error.exception.code, 400)

    def test_night_starts_three_hours_after_last_rapid_injection(self):
        dinner = datetime(2026, 9, 22, 18, tzinfo=timezone.utc)
        correction = datetime(2026, 9, 22, 22, tzinfo=timezone.utc)
        breakfast = datetime(2026, 9, 23, 6, tzinfo=timezone.utc)
        treatments = [
            {"at": dinner, "insulin": 4, "role": "meal"},
            {"at": correction, "insulin": 1, "role": "correction"},
            {"at": dinner + timedelta(hours=1), "insulin": 8, "eventType": "Basal Injection"},
            {"id": "breakfast", "at": breakfast, "insulin": 3, "role": "meal", "carbs": 50, "eventType": "Meal Bolus", "notes": ""},
        ]
        entries = [
            {"at": correction + timedelta(hours=3), "sgv": 150},
            {"at": breakfast, "sgv": 110},
            {"at": correction + timedelta(hours=4), "sgv": 95},
        ]
        meals = [{"meal": "dinner", "at": dinner}]
        settings = {"timezone": "Europe/Berlin", "window_minutes": 180}
        night = night_analysis("2026-09-22", treatments, entries, meals, settings)
        self.assertEqual(night["last_injection_at"], correction)
        self.assertEqual(night["start"], correction + timedelta(hours=3))
        self.assertEqual(night["delta_mgdl"], -40)
        self.assertEqual(night["minimum_glucose"], 95)

    def test_breakfast_work_free_defaults_and_day_override(self):
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        monday = (today - timedelta(days=today.weekday())).isoformat()
        saturday = (today - timedelta(days=(today.weekday() - 5) % 7)).isoformat()
        meals_by_day = {
            monday: [{"id": "work-breakfast", "meal": "breakfast", "at": monday + "T08:00:00+02:00",
                      "calculation": {"factor_units_per_ke": "1", "ratio_g_per_unit": "10",
                                      "carbs_times_isf_total": "500", "insulin_effect_mgdl": "0",
                                      "delta_mgdl": "50", "carb_events": [
                                          {"at": monday + "T08:00:00+02:00", "carbs": "10", "isf": "50"}]}}],
            saturday: [{"id": "free-breakfast", "meal": "breakfast", "at": saturday + "T08:00:00+02:00",
                        "calculation": {"factor_units_per_ke": "2", "ratio_g_per_unit": "5",
                                        "carbs_times_isf_total": "500", "insulin_effect_mgdl": "0",
                                        "delta_mgdl": "100", "carb_events": [
                                            {"at": saturday + "T08:00:00+02:00", "carbs": "10", "isf": "50"}]}}],
        }

        def payload(day, refresh=False):
            return {"meals": meals_by_day.get(day, [])}

        with fake_days(side_effect=payload):
            initial = self.request("/api/summary?days=7")["groups"]["breakfast"]
            self.assertEqual(initial["by_day_type"]["work"]["mean_units_per_ke"], "1")
            self.assertEqual(initial["by_day_type"]["free"]["mean_units_per_ke"], "2")
            self.assertEqual(initial["count"], 2)
            self.request("/api/analysis/day-type", method="POST", body={"day": monday, "day_type": "free"})
            changed = self.request("/api/summary?days=7")["groups"]["breakfast"]
            self.assertEqual(changed["by_day_type"]["work"]["count"], 0)
            self.assertEqual(changed["by_day_type"]["free"]["mean_units_per_ke"], "1.5")
            self.assertEqual(changed["count"], 2)
            self.assertEqual([item["expected_delta_mgdl"] for item in changed["calculations"]], ["50", "50"])
            # Saturday may lie before or after Monday depending on the weekday the test runs.
            self.assertEqual(sorted(item["deviation_mgdl"] for item in changed["calculations"]), ["0", "50"])
            self.assertEqual(marks.analysis_day_types()[monday], "free")
            self.request("/api/analysis/exclusion", method="POST", body={
                "meal_id": "work-breakfast", "day": monday, "excluded": True,
            })
            filtered = self.request("/api/summary?days=7")["groups"]["breakfast"]
            self.assertEqual(filtered["by_day_type"]["free"]["count"], 1)
            self.assertEqual(filtered["by_day_type"]["free"]["excluded_count"], 1)
            self.assertEqual(filtered["by_day_type"]["free"]["mean_units_per_ke"], "2")
            self.assertEqual([item["expected_delta_mgdl"] for item in filtered["calculations"]], ["50", "50"])

    def test_vacation_marks_every_day_free_until_switched_off(self):
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        monday = (today - timedelta(days=today.weekday())).isoformat()
        self.assertFalse(self.request("/api/settings")["vacation"]["active"])
        state = self.request("/api/vacation", method="POST", body={"active": True})
        self.assertEqual(state, {"active": True, "since": today.isoformat()})
        self.assertEqual(marks.day_type_for(today.isoformat(), {}, marks.vacation_periods()),
                         ("free", "vacation"))
        self.assertEqual(marks.day_type_for("2020-01-06", {}, marks.vacation_periods()),
                         ("work", "weekday"))
        self.assertEqual(marks.day_type_for(today.isoformat(), {today.isoformat(): "work"},
                                              marks.vacation_periods()), ("work", "manual"))
        day = self.request("/api/day?date=" + today.isoformat())
        self.assertEqual((day["day_type"], day["day_type_source"]), ("free", "vacation"))
        # Switching off on the day it was switched on leaves no vacation behind.
        self.request("/api/vacation", method="POST", body={"active": False})
        self.assertEqual(marks.vacation_periods(), [])
        self.assertEqual(marks.day_type_for(monday, {}, []), ("work", "weekday"))
        # An older vacation ends the day before it is switched off.
        marks.set_vacation(True, "2026-08-01")
        marks.set_vacation(False, "2026-08-10")
        periods = marks.vacation_periods()
        self.assertEqual(periods, [{"start_day": "2026-08-01", "end_day": "2026-08-09"}])
        self.assertEqual(marks.day_type_for("2026-08-03", {}, periods)[0], "free")
        self.assertEqual(marks.day_type_for("2026-08-10", {}, periods)[0], "work")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/vacation", method="POST", body={"active": "yes"})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_breakfast_dose_help_uses_median_factor_of_selected_day_type(self):
        self.request("/api/settings")
        zone = ZoneInfo("Europe/Berlin")
        now = datetime.now(timezone.utc)
        local_today = now.astimezone(zone).date()
        # Make every time of day count as breakfast for this test.
        _, settings = config.get_settings()
        config.save_settings({**settings, "breakfast_end_minute": 1439, "lunch_end_minute": 1439})
        current_day = {
            "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 100}],
            "treatments": [],
        }
        past = [(local_today - timedelta(days=offset)).isoformat() for offset in range(1, 29)]
        free_days = [day for day in past if marks.day_type_for(day, {}, [])[0] == "free"][:3]
        factors = dict(zip(free_days, ["2", "3", "4"]))

        def payload(day, refresh=True):
            if day == local_today.isoformat():
                return {**current_day, "meals": []}
            factor = factors.get(day)
            return {"meals": [{"id": "b-" + day, "meal": "breakfast",
                               "calculation": {"factor_units_per_ke": factor}}] if factor else []}

        with fake_days(side_effect=payload):
            self.request("/api/analysis/day-type", method="POST",
                         body={"day": local_today.isoformat(), "day_type": "work"})
            work = self.request("/api/dose-help", method="POST", body={"carbs": "30"})
            self.assertEqual(work["day_type"], "work")
            self.assertEqual(work["factor_source"], "nightscout")
            self.assertEqual(work["breakfast_factor_count"], 0)
            self.request("/api/analysis/day-type", method="POST",
                         body={"day": local_today.isoformat(), "day_type": "free"})
            free = self.request("/api/dose-help", method="POST", body={"carbs": "30"})
        self.assertEqual(free["day_type_source"], "manual")
        self.assertEqual(free["factor_source"], "breakfast_day_type")
        self.assertEqual(free["breakfast_factor_count"], 3)
        self.assertEqual(Decimal(free["units_per_ke"]), Decimal(3))
        self.assertEqual(Decimal(free["carb_units"]), Decimal(9))
        self.assertEqual(Decimal(free["nightscout_units_per_ke"]), Decimal(work["units_per_ke"]))

    def test_dose_help_auto_value_uses_cautious_percentile_of_current_meal(self):
        self.request("/api/settings")
        updated = self.request("/api/settings", method="POST",
                               body={"dose_cautious_target_mgdl": "150", "auto_percentile": "37.5"})
        self.assertEqual(Decimal(updated["settings"]["dose_cautious_target_mgdl"]), Decimal(150))
        for invalid in ({"dose_cautious_target_mgdl": "90"}, {"dose_cautious_target_mgdl": "251"},
                        {"auto_percentile": "24"}, {"auto_percentile": "51"}, {"auto_percentile": "NaN"},
                        {"auto_lookback_days": 21}, {"auto_half_life_days": "0.5"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/settings", method="POST", body=invalid)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        zone = ZoneInfo("Europe/Berlin")
        now = datetime.now(timezone.utc)
        local_today = now.astimezone(zone).date()
        # Make every time of day count as dinner for this test.
        _, settings = config.get_settings()
        config.save_settings({**settings, "breakfast_end_minute": 1, "lunch_end_minute": 2})
        current_day = {
            "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 100}],
            "treatments": [],
        }
        past = [(local_today - timedelta(days=offset)).isoformat() for offset in range(1, 6)]
        factors = dict(zip(past, ["1.5", "0.8", "1.2", "1.0", "1.1"]))

        def payload(day, refresh=True):
            if day == local_today.isoformat():
                return {**current_day, "meals": []}
            factor = factors.get(day)
            return {"meals": [
                {"id": "d-" + day, "meal": "dinner", "calculation": {"factor_units_per_ke": factor}},
                {"id": "l-" + day, "meal": "lunch", "calculation": {"factor_units_per_ke": "9"}},
            ] if factor else []}

        with fake_days(side_effect=payload):
            result = self.request("/api/dose-help", method="POST", body={"carbs": "20"})
            marks.set_analysis_exclusion("d-" + past[0], True)
            marks.set_analysis_exclusion("d-" + past[1], True)
            too_few = self.request("/api/dose-help", method="POST", body={"carbs": "20"})
        self.assertEqual(result["auto_meal"], "dinner")
        self.assertEqual(result["auto_factor_count"], 5)
        # 37.5 % percentile of the five dinners, each weighted by 0.5 ** (age / 7 days).
        expected = forecast.weighted_quantile(
            [(Decimal(value), forecast.weight(age, Decimal(7)))
             for age, value in enumerate(["1.5", "0.8", "1.2", "1.0", "1.1"], start=1)], Decimal("0.375"))
        self.assertEqual(Decimal(result["auto_units_per_ke"]), expected)
        self.assertTrue(Decimal("1.0") < expected < Decimal("1.1"))
        self.assertEqual(Decimal(result["auto_median_units_per_ke"]), Decimal("1.1"))
        self.assertEqual(Decimal(result["auto_q1_units_per_ke"]), Decimal("1.0"))
        self.assertEqual(Decimal(result["cautious_target_mgdl"]), Decimal(150))
        self.assertEqual(Decimal(result["auto_total_units"]), 2 * expected)
        self.assertEqual(Decimal(result["auto_entry_units"]), Decimal("2"))
        self.assertEqual(result["factor_source"], "nightscout")
        self.assertEqual(too_few["auto_factor_count"], 3)
        self.assertNotIn("auto_entry_units", too_few)

    def test_summary_shows_factor_used_for_dose_suggestion(self):
        FakeNightscout.profile["store"]["Default"]["carbratio"] = [
            {"time": "00:00", "timeAsSeconds": 0, "value": 12.5}]
        self.request("/api/profile/sync", method="POST", body={})
        self.request("/api/settings", method="POST", body={"auto_half_life_days": "28"})
        local_today = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Berlin")).date()
        past = [(local_today - timedelta(days=offset)).isoformat() for offset in range(1, 6)]
        factors = dict(zip(past, ["1.5", "0.8", "1.2", "1.0", "1.1"]))

        def payload(day, refresh=True):
            factor = "9" if day == local_today.isoformat() else factors.get(day)
            return {"meals": [{"id": "d-" + day, "meal": "dinner", "at": day + "T18:00:00+02:00",
                               "calculation": {"factor_units_per_ke": factor,
                                               "ratio_g_per_unit": "10"}}] if factor else []}

        with fake_days(side_effect=payload):
            summary = self.request("/api/summary?days=3")
        dinner = summary["groups"]["dinner"]
        expected = forecast.weighted_quantile(
            [(Decimal(value), forecast.weight(age, Decimal(28)))
             for age, value in enumerate(["1.5", "0.8", "1.2", "1.0", "1.1"], start=1)], Decimal("0.375"))
        self.assertEqual(Decimal(dinner["suggestion"]["auto_units_per_ke"]), expected)
        self.assertEqual(dinner["suggestion"]["count"], 5)
        self.assertEqual(dinner["suggestion"]["days"], 30)
        self.assertEqual([Decimal(value) for value in dinner["nightscout_units_per_ke"]], [Decimal("0.8")])
        self.assertIsNone(summary["groups"]["lunch"]["suggestion"]["auto_units_per_ke"])
        for kind in ("work", "free"):
            self.assertIn("suggestion", summary["groups"]["breakfast"]["by_day_type"][kind])
        ratio = summary["day_type_ratio"]
        self.assertEqual((ratio["days"], ratio["reference_days"], ratio["neighbours"]), (30, 7, 2))
        self.assertEqual(list(ratio["meals"]), ["breakfast"])
        derivation = summary["suggestion_derivation"]
        self.assertEqual((derivation["lookback_days"], Decimal(derivation["half_life_days"])), (30, Decimal(28)))
        self.assertEqual(derivation["backtest_days"], 90)
        variants = derivation["variants"]
        self.assertEqual([(item["lookback_days"], Decimal(item["half_life_days"])) for item in variants],
                         [(days, Decimal(half)) for days in (8, 15, 30) for half in (1, 2, 3, 5, 7, 14, 28)])
        self.assertEqual([str(cell["percentile"]) for cell in variants[0]["cells"]], ["25", "37.5", "50", "surcharge"])
        self.assertEqual((derivation["glucose_low_mgdl"], derivation["glucose_high_mgdl"]), ("80", "160"))

    def test_summary_shows_median_middle_half_and_full_range(self):
        factors = ["1", "1.1", "1.2", "1.3", "5"]
        meals = [{
            "id": f"meal-{index}",
            "meal": "lunch",
            "at": f"2026-09-{10 + index}T12:00:00+00:00",
            "calculation": {
                "factor_units_per_ke": factor,
                "ratio_g_per_unit": str(10 / float(factor)),
            },
        } for index, factor in enumerate(factors)]
        with fake_days(side_effect=then_empty([{"meals": meals}])):
            group = self.request("/api/summary?days=14")["groups"]["lunch"]
        self.assertEqual(group["count"], 5)
        self.assertEqual(group["median_units_per_ke"], "1.2")
        self.assertEqual(float(group["mean_units_per_ke"]), 1.92)
        self.assertEqual(float(group["q1_units_per_ke"]), 1.1)
        self.assertEqual(float(group["q3_units_per_ke"]), 1.3)
        self.assertEqual(group["min_units_per_ke"], "1")
        self.assertEqual(group["max_units_per_ke"], "5")
        self.assertEqual([item["units_per_ke"] for item in group["calculations"]], factors)
        self.assertEqual(group["strong_outlier_count"], 1)
        self.assertEqual([item["strong_outlier"] for item in group["calculations"]], [False, False, False, False, True])

        with fake_days(return_value={"meals": meals}):
            response = self.request("/api/analysis/exclusion", method="POST", body={
                "meal_id": "meal-4", "day": "2026-09-23", "excluded": True,
            })
        self.assertTrue(response["excluded"])
        self.assertIn("meal-4", marks.analysis_exclusions())
        with fake_days(side_effect=then_empty([{"meals": meals}])):
            filtered = self.request("/api/summary?days=14")["groups"]["lunch"]
        self.assertEqual(filtered["count"], 4)
        self.assertEqual(filtered["excluded_count"], 1)
        self.assertEqual(filtered["valid_count"], 5)
        self.assertEqual(float(filtered["mean_units_per_ke"]), 1.15)
        self.assertEqual(filtered["max_units_per_ke"], "1.3")
        self.assertTrue(filtered["calculations"][-1]["excluded"])
        self.assertTrue(filtered["calculations"][-1]["strong_outlier"])

        with fake_days(return_value={"meals": meals}):
            self.request("/api/analysis/exclusion", method="POST", body={
                "meal_id": "meal-4", "day": "2026-09-23", "excluded": False,
            })
        self.assertNotIn("meal-4", marks.analysis_exclusions())

        with fake_days(side_effect=then_empty([{"meals": meals[:4]}])):
            small_group = self.request("/api/summary?days=14")["groups"]["lunch"]
        self.assertIsNone(small_group["q1_units_per_ke"])
        self.assertIsNone(small_group["q3_units_per_ke"])
        self.assertEqual(float(small_group["median_units_per_ke"]), 1.15)

        with fake_days(side_effect=then_empty([{"meals": meals[:2]}])):
            sparse_group = self.request("/api/summary?days=14")["groups"]["lunch"]
        self.assertEqual(sparse_group["strong_outlier_count"], 0)

    def test_small_carb_entry_defaults_to_snack_of_no_meal(self):
        self.request("/api/day?date=2026-09-23")
        small = {"day": "2026-09-23", "at": "2026-09-23T15:30:00Z", "mode": "meal", "carbs": "10"}
        preview = self.request("/api/entry/preview", method="POST", body=small)
        self.assertEqual({key: preview["assignment"][key] for key in ("meal", "snack", "own_meal", "own_meal_taken")},
                         {"meal": None, "snack": True, "own_meal": "dinner", "own_meal_taken": False})
        flipped = self.request("/api/entry/preview", method="POST", body={**small, "as": "meal"})
        self.assertFalse(flipped["assignment"]["snack"])
        large = self.request("/api/entry/preview", method="POST", body={**small, "carbs": "20"})
        self.assertIsNone(large["assignment"])

        saved = self.request("/api/entry", method="POST", body=small)
        self.assertEqual(saved["kind"], "snack")
        self.assertEqual(saved["day_view"]["meals"], [])
        # The real dinner later is still the day's dinner.
        dinner = {**small, "at": "2026-09-23T17:45:00Z", "carbs": "66", "insulin": "5"}
        self.assertIsNone(self.request("/api/entry/preview", method="POST", body=dinner)["assignment"])
        saved = self.request("/api/entry", method="POST", body=dinner)
        self.assertEqual((saved["kind"], [meal["meal"] for meal in saved["day_view"]["meals"]]), ("meal", ["dinner"]))
        self.assertEqual(len(self.request("/api/day?date=2026-09-23")["meals"]), 1)

    def test_entry_preview_offers_sport_basal_and_day_type_and_save_stores_all_in_one_call(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "exercise", "at": "2026-09-23T06:00:00Z", "sport_kind": "gym", "duration_minutes": 30,
        })
        self.request("/api/day?date=2026-09-23")  # The form previews the loaded day.
        entry = {"day": "2026-09-23", "at": "2026-09-23T07:00:00Z", "mode": "meal", "carbs": "40"}
        preview = self.request("/api/entry/preview", method="POST", body=entry)
        self.assertIsNone(preview["assignment"])
        self.assertEqual([item["sport_type"] for item in preview["sport"]["events"]], ["gym"])
        self.assertEqual(Decimal(preview["sport"]["suggested_change_mgdl"]), Decimal(-40))
        self.assertEqual(Decimal(preview["basal_units"]), Decimal(17))
        self.assertEqual(preview["day_type"], {"value": "work", "source": "weekday"})
        self.assertEqual(preview["dose"]["message"], "Eine aktuelle Empfehlung gibt es nur für den heutigen Tag.")
        later = self.request("/api/entry/preview", method="POST", body={**entry, "at": "2026-09-23T13:00:00Z"})
        self.assertEqual((later["basal_units"], later["day_type"], later["sport"]), ("0", None, None))

        saved = self.request("/api/entry", method="POST", body={
            **entry, "insulin": "3", "consider_sport": True, "sport_effect_mgdl": "-25", "with_basal": True,
            "carbs_certainty": "known"})
        self.assertEqual((saved["kind"], saved["sport_marked"], saved["basal_error"]), ("meal", True, None))
        self.assertTrue(saved["basal"]["nightscout_synced"])
        notes = [item["notes"] for item in FakeNightscout.treatments]
        self.assertTrue(any("[DA2:basal]" in note for note in notes))
        meal = saved["day_view"]["meals"][0]
        self.assertEqual(meal["source_id"], saved["id"])
        self.assertEqual((meal["sport"]["status"], Decimal(meal["sport"]["effect_mgdl"])), ("affected", Decimal(25)))
        self.assertEqual(len(saved["day_view"]["basal"]), 1)

        snack = {**entry, "at": "2026-09-23T07:30:00Z", "carbs": "10"}
        preview = self.request("/api/entry/preview", method="POST", body=snack)
        self.assertEqual((preview["assignment"]["meal"], preview["assignment"]["snack"]), ("breakfast", True))
        self.assertEqual(preview["basal_units"], "0")
        # A snack can count the sport too; it is marked on the main meal whose window holds it.
        self.assertEqual([item["sport_type"] for item in preview["sport"]["events"]], ["gym"])
        # Breakfast is taken, so the entry cannot be flipped to its own breakfast.
        own = self.request("/api/entry/preview", method="POST", body={**snack, "as": "meal"})
        self.assertEqual((own["assignment"]["snack"], own["assignment"]["own_meal_taken"]), (True, True))
        saved = self.request("/api/entry", method="POST", body={
            **snack, "consider_sport": True, "sport_effect_mgdl": "-30"})
        self.assertEqual((saved["kind"], saved["sport_marked"], saved["sport_error"]), ("snack", True, None))
        meal = saved["day_view"]["meals"][0]
        self.assertEqual((meal["sport"]["status"], Decimal(meal["sport"]["effect_mgdl"])), ("affected", Decimal(30)))
        self.assertEqual(FakeNightscout.treatments[-1]["eventType"], "Carb Correction")
        self.assertIn("[DA2:snack]", FakeNightscout.treatments[-1]["notes"])

        # Lunch at 12:00 is taken: at 16:00, after its window, an entry is a snack of no meal.
        self.request("/api/entry", method="POST", body={**entry, "at": "2026-09-23T10:00:00Z", "insulin": "3"})
        late = {**entry, "at": "2026-09-23T14:00:00Z", "carbs": "10"}
        self.request("/api/treatment", method="POST", body={
            "kind": "exercise", "at": "2026-09-23T13:00:00Z", "sport_kind": "jogging", "duration_minutes": 30,
        })
        preview = self.request("/api/entry/preview", method="POST", body={**late, "as": "meal"})
        self.assertEqual({key: preview["assignment"][key] for key in ("meal", "at", "snack", "own_meal_taken")},
                         {"meal": None, "at": None, "snack": True, "own_meal_taken": True})
        self.request("/api/day?date=2026-09-23")
        self.assertIsNotNone(self.request("/api/entry/preview", method="POST", body=late)["sport"])
        saved = self.request("/api/entry", method="POST", body={
            **late, "insulin": "1", "consider_sport": True, "sport_effect_mgdl": "-20"})
        # A snack of no meal has no meal to carry the sport; it only lowers its own dose.
        self.assertEqual((saved["kind"], saved["sport_marked"], saved["sport_error"]), ("snack", False, None))
        self.assertTrue(all(meal["sport"].get("status") != "affected" for meal in saved["day_view"]["meals"][1:]))
        self.assertEqual([meal["meal"] for meal in saved["day_view"]["meals"]], ["breakfast", "lunch"])
        row = next(item for item in saved["day_view"]["timeline"] if item["id"] == saved["id"])
        self.assertEqual((row["kind"], row["meal_id"]), ("snack", None))

    def test_place_is_suggested_from_position_stored_locally_and_unknown_positions_join_a_place(self):
        self.request("/api/settings")
        canteen = self.request("/api/places", method="POST", body={"action": "create", "name": "Kantine"})
        canteen_id = canteen["places"][0]["id"]
        self.request("/api/places", method="POST", body={
            "action": "add_spot", "place_id": canteen_id, "lat": 49.0, "lon": 8.4})
        view = self.request("/api/places", method="POST", body={"action": "create", "name": "Restaurant"})
        restaurant_id = next(place["id"] for place in view["places"] if place["name"] == "Restaurant")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        today = now.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        here = {"lat": 49.0005, "lon": 8.4, "accuracy_m": 20, "captured_at": now.isoformat()}
        entry = {"day": today, "at": now.isoformat(), "mode": "meal", "carbs": "50", "position": here}
        place = self.request("/api/entry/preview", method="POST", body=entry)["place"]
        self.assertEqual((place["status"], place["place_id"]), ("matched", canteen_id))
        self.assertEqual([option["name"] for option in place["options"]], ["Kantine", "Restaurant"])
        self.request("/api/places", method="POST", body={"action": "quick", "id": restaurant_id, "quick": False})
        options = self.request("/api/entry/preview", method="POST", body=entry)["place"]["options"]
        self.assertEqual([(option["name"], option["quick"]) for option in options],
                         [("Kantine", True), ("Restaurant", False)])
        backdated = self.request("/api/entry/preview", method="POST", body={
            **entry, "at": (now - timedelta(hours=3)).isoformat()})["place"]
        self.assertEqual((backdated["status"], backdated["place_id"]), ("offset", None))

        saved = self.request("/api/entry", method="POST", body={**entry, "insulin": "5", "place_id": canteen_id})
        row = next(item for item in saved["day_view"]["timeline"] if item["id"] == saved["id"])
        self.assertEqual(row["place"], {"id": canteen_id, "name": "Kantine"})
        self.assertEqual(saved["day_view"]["meals"][0]["place"]["name"], "Kantine")
        # Nightscout never sees a place or a position.
        self.assertNotIn("Kantine", json.dumps(FakeNightscout.treatments))
        self.assertNotIn("49.0005", json.dumps(FakeNightscout.treatments))

        # A position that fits no place waits as unknown until it joins a place.
        away = {**here, "lat": 49.1}
        snack = self.request("/api/entry", method="POST", body={**entry, "carbs": "10", "position": away})
        unknown = self.request("/api/places")["unknown"]
        self.assertEqual([group["count"] for group in unknown], [1])
        self.assertEqual(unknown[0]["nearest_place"]["name"], "Kantine")
        view = self.request("/api/places", method="POST", body={
            "action": "add_spot", "place_id": restaurant_id, "lat": unknown[0]["lat"], "lon": unknown[0]["lon"]})
        self.assertEqual(view["unknown"], [])
        self.assertEqual({place["name"]: place["count"] for place in view["places"]}, {"Kantine": 1, "Restaurant": 1})
        day = self.request("/api/day?date=" + today)
        self.assertEqual(next(item for item in day["timeline"] if item["id"] == snack["id"])["place"]["name"], "Restaurant")

        # The edit dialog changes the place afterwards and keeps the stored position.
        self.request("/api/treatment", method="POST", body={
            "id": saved["id"], "original_day": today, "kind": "meal", "at": now.isoformat(),
            "carbs": "50", "insulin": "5", "place_id": restaurant_id})
        self.request("/api/day?date=" + today)  # The edit is mirrored on the next day load.
        meal = self.request("/api/v1/meals?days=1")["rows"][0]
        self.assertEqual(meal["place"], "Restaurant")
        # Deleting a place turns entries with a position back into unknown positions.
        view = self.request("/api/places", method="POST", body={"action": "delete", "id": restaurant_id})
        self.assertEqual(sum(group["count"] for group in view["unknown"]), 2)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/places", method="POST", body={"action": "create", "name": "kantine"})
        # A chip picked for a precise position that fits no spot learns it as a new spot.
        self.request("/api/entry", method="POST", body={
            **entry, "carbs": "20", "position": {**here, "lat": 49.3}, "place_id": canteen_id})
        view = self.request("/api/places")
        self.assertEqual(len(view["places"][0]["spots"]), 2)
        self.assertEqual(view["places"][0]["count"], 1)
        # A coarse position is stored but never grouped with the unknown ones.
        self.request("/api/entry", method="POST", body={
            **entry, "carbs": "15", "position": {**here, "lat": 49.4, "accuracy_m": 900}})
        self.assertEqual(sum(group["count"] for group in self.request("/api/places")["unknown"]), 2)

    def test_finger_value_alone_is_saved_as_bg_check(self):
        self.request("/api/day?date=2026-09-23")
        body = {"day": "2026-09-23", "at": "2026-09-23T07:00:00Z", "mode": "meal", "carbs": "",
                "finger_mgdl": "143"}
        saved = self.request("/api/entry", method="POST", body=body)
        self.assertEqual((saved["kind"], saved["finger_mgdl"]), ("finger", "143"))
        check = FakeNightscout.treatments[-1]
        self.assertEqual((check["eventType"], check["glucose"], check["glucoseType"]), ("BG Check", 143.0, "Finger"))
        again = self.request("/api/entry", method="POST", body={**body, "event_id": saved["event_id"]})
        self.assertEqual(sum(item["eventType"] == "BG Check" for item in FakeNightscout.treatments), 1)
        row = next(item for item in again["day_view"]["timeline"] if item["kind"] == "finger")
        self.assertEqual(Decimal(row["glucose_mgdl"]), Decimal(143))
        with self.assertRaises(Exception):
            self.request("/api/entry", method="POST", body={**body, "finger_mgdl": "5"})

    def test_tapped_suggestion_is_saved_only_while_it_is_still_current(self):
        self.request("/api/settings")
        now = datetime.now(timezone.utc)
        today = now.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        current_day = {
            "day": today, "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 150}],
            "treatments": [], "basal": [], "day_type": "work", "day_type_source": "weekday", "meals": [],
        }
        entry = {"day": today, "at": now.isoformat(), "mode": "meal", "carbs": "20"}
        with fake_days(return_value=current_day):
            dose = self.request("/api/entry/preview", method="POST", body=entry)["dose"]
            self.assertEqual([item["variant"] for item in dose["options"]], ["normal", "cautious", "carbs"])
            self.assertEqual(dose["options"][0]["units"], dose["entry_units"])
            current_day["entries"][0]["sgv"] = 190
            with self.assertRaises(urllib.error.HTTPError) as changed:
                self.request("/api/entry", method="POST", body={
                    **entry, "insulin": dose["entry_units"], "dose_check": dose["check"]})
            self.assertIn("Empfehlung geändert", json.loads(changed.exception.read())["error"])
            changed.exception.close()
            self.assertEqual(FakeNightscout.treatments, [])
            fresh = self.request("/api/entry/preview", method="POST", body=entry)["dose"]
            self.request("/api/entry", method="POST", body={
                **entry, "insulin": fresh["entry_units"], "dose_check": fresh["check"]})
        self.assertEqual(Decimal(str(FakeNightscout.treatments[0]["insulin"])), Decimal(fresh["entry_units"]))

    def test_lower_target_is_offered_only_without_carb_uncertainty(self):
        self.request("/api/settings")
        self.request("/api/settings", method="POST", body={"dose_target_mgdl": "120"})
        now = datetime.now(timezone.utc)
        today = now.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        current_day = {
            "day": today, "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 190}],
            "treatments": [], "basal": [], "day_type": "work", "day_type_source": "weekday", "meals": [],
        }
        entry = {"day": today, "at": now.isoformat(), "mode": "meal", "carbs": "", "as": "meal"}
        variants = lambda dose: [item["variant"] for item in dose["options"]]
        # The lower target is offered by day only; the test runs at any hour.
        with fake_days(return_value=current_day), patch.object(dosing, "LOW_TARGET_HOURS", (0, 24)):
            correction = self.request("/api/entry/preview", method="POST", body=entry)["dose"]
            self.assertEqual(variants(correction), ["normal", "low", "cautious"])
            low = correction["options"][1]
            self.assertGreater(Decimal(low["units"]), Decimal(correction["options"][0]["units"]))
            self.assertIsNone(correction["low_note"])
            small = self.request("/api/entry/preview", method="POST",
                                 body={**entry, "carbs": "20", "carbs_certainty": "known"})["dose"]
            self.assertEqual(variants(small), ["normal", "low", "cautious", "carbs"])
            unsure = self.request("/api/entry/preview", method="POST",
                                  body={**entry, "carbs": "20", "carbs_certainty": "unsure"})["dose"]
            self.assertEqual(variants(unsure), ["normal", "cautious", "carbs"])
            self.assertEqual(unsure["low_note"], "KH unsicher")
            fat = self.request("/api/entry/preview", method="POST",
                               body={**entry, "carbs": "20", "fat_protein": True})["dose"]
            self.assertEqual(fat["low_blocked"], "fat")
            meal = self.request("/api/entry/preview", method="POST", body={**entry, "carbs": "60"})["dose"]
            self.assertEqual(variants(meal), ["normal", "cautious", "carbs"])
            self.assertIsNone(meal["low_note"])
            # A tapped lower-target suggestion saves like any other.
            self.request("/api/entry", method="POST", body={
                **entry, "insulin": low["units"], "dose_check": correction["check"], "dose_variant": "low"})
        self.assertEqual(Decimal(str(FakeNightscout.treatments[0]["insulin"])), Decimal(low["units"]))

    def test_tapped_suggestion_saves_while_earlier_insulin_still_acts(self):
        # What still acts decays every second; the shown doses stay the same, so saving must not fail.
        self.request("/api/settings")
        now = datetime.now(timezone.utc)
        today = now.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        current_day = {
            "day": today, "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 150}],
            "treatments": [{"id": "earlier", "at": (now - timedelta(minutes=40)).isoformat(),
                            "eventType": "Correction Bolus", "insulin": 3, "carbs": 0, "notes": ""}],
            "basal": [], "day_type": "work", "day_type_source": "weekday", "meals": [],
        }
        entry = {"day": today, "at": now.isoformat(), "mode": "meal", "carbs": "144", "as": "meal",
                 "fat_protein": True}
        with fake_days(return_value=current_day):
            preview = self.request("/api/entry/preview", method="POST", body=entry)
            self.assertIsNotNone(preview["on_board"])
            normal = preview["dose"]["options"][0]
            time.sleep(1.1)
            self.request("/api/entry", method="POST", body={
                **entry, "insulin": normal["units"], "dose_check": preview["dose"]["check"],
                "dose_variant": "normal"})
        self.assertEqual(Decimal(str(FakeNightscout.treatments[-1]["insulin"])), Decimal(normal["units"]))

    def test_large_meal_is_split_and_the_second_part_is_offered_later(self):
        self.request("/api/settings")
        now = datetime.now(timezone.utc)
        today = now.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        current_day = {
            "day": today, "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 120}],
            "treatments": [], "basal": [], "day_type": "work", "day_type_source": "weekday", "meals": [],
        }
        entry = {"day": today, "at": now.isoformat(), "mode": "meal", "carbs": "100", "as": "meal"}
        with fake_days(return_value=current_day):
            preview = self.request("/api/entry/preview", method="POST", body=entry)
            self.assertEqual((preview["split"]["active"], preview["split"]["first_percent"]), (True, 60))
            normal = preview["dose"]["options"][0]
            full, first, later = (Decimal(normal[key]) for key in ("full_units", "units", "later_units"))
            self.assertEqual(first + later, full)
            self.assertEqual(first, dosing.half_units(full * Decimal("0.6")))
            off = self.request("/api/entry/preview", method="POST", body={**entry, "split": False})
            self.assertFalse(off["split"]["active"])
            self.assertNotIn("later_units", off["dose"]["options"][0])
            self.assertIsNone(self.request("/api/entry/preview", method="POST", body={**entry, "carbs": "40"})["split"])
            saved = self.request("/api/entry", method="POST", body={
                **entry, "insulin": str(first), "dose_variant": "normal"})
        self.assertEqual(Decimal(saved["split_units"]), later)
        note = FakeNightscout.treatments[-1]["notes"]
        self.assertIn(episodes.split_tag(unit_text(later), 120), note)

        meal_at = now - timedelta(minutes=110)
        current_day["treatments"] = [{"at": meal_at.isoformat(), "role": "meal", "eventType": "Meal Bolus",
                                      "carbs": "100", "insulin": str(first), "split": episodes.split_plan(note)}]
        with fake_days(return_value=current_day):
            dose = self.request("/api/entry/preview", method="POST", body={**entry, "carbs": "0"})["dose"]
        self.assertEqual(dose["options"][0]["variant"], "split")
        self.assertEqual(Decimal(dose["options"][0]["units"]), later)
        self.assertEqual(dose["split_plan"]["due_at"], (meal_at + timedelta(minutes=120)).isoformat())

    def test_window_editor_reads_cgm_per_minute_and_counts_a_candidate_window(self):
        self.request("/api/treatment", method="POST", body={
            "kind": "meal", "at": MEAL_AT, "carbs": "40", "insulin": "4",
        })
        self.request("/api/treatment", method="POST", body={
            "kind": "correction", "at": "2026-09-23T12:30:00Z", "insulin": "1",
        })
        meal = self.request("/api/day?date=2026-09-23")["meals"][0]
        self.assertEqual(meal["window"]["start_glucose"], {"value": "100", "tone": "neutral"})
        query = urllib.parse.urlencode({"day": "2026-09-23", "meal_id": meal["id"]})
        editor = self.request("/api/analysis/window-editor?" + query)
        self.assertEqual(editor["limits"], {"lead_minutes": 120, "lag_minutes": 120, "min_minutes": 30, "max_minutes": 600})
        readings = {row["t"]: row for row in editor["readings"]}
        self.assertEqual(Decimal(readings["2026-09-23T10:00:00+00:00"]["value"]), Decimal(100))
        self.assertIsNone(readings["2026-09-23T11:00:00+00:00"]["value"])
        self.assertIn("2026-09-23T12:30:00+00:00", readings)
        body = {"day": "2026-09-23", "meal_id": meal["id"], "start": MEAL_AT}
        whole = self.request("/api/analysis/window/preview", method="POST", body={**body, "end": "2026-09-23T13:00:00Z"})
        self.assertEqual((Decimal(whole["carbs_g"]), Decimal(whole["rapid_units"])), (Decimal(40), Decimal(5)))
        self.assertEqual((Decimal(whole["delta_mgdl"]), whole["minutes"], whole["problem"]), (Decimal(20), 180, None))
        short = self.request("/api/analysis/window/preview", method="POST", body={**body, "end": "2026-09-23T12:00:00Z"})
        self.assertEqual((Decimal(short["rapid_units"]), short["missing_glucose"]), (Decimal(4), True))
        early = self.request("/api/analysis/window/preview", method="POST", body={**body, "end": "2026-09-23T10:10:00Z"})
        self.assertIn("30 Minuten", early["problem"])


if __name__ == "__main__":
    unittest.main()
