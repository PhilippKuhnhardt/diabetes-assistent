import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import storage
from app.server import Handler, night_analysis


MEAL_AT = "2026-09-23T10:00:00Z"


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
        self.old_db = storage.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        storage.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        storage.init()
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
        storage.DB_PATH = self.old_db
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
        self.assertEqual(Decimal(result["trend_delta_mgdl"]), Decimal(10))
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

    def test_dose_help_suggestion_can_be_written_once_to_nightscout(self):
        self.request("/api/settings")  # Sync the current Nightscout I:C and KF schedules.
        now = datetime.now(timezone.utc)
        current_day = {
            "entries": [{"at": (now - timedelta(minutes=2)).isoformat(), "sgv": 150}],
            "treatments": [],
        }
        with patch("app.server.day_payload", return_value=current_day):
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
        self.assertEqual(storage.analysis_exclusions(), set())

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
        _, settings = storage.get_settings()
        self.assertEqual(settings["isf_schedule"][0]["mgdl_per_unit"], "50")

        refreshed = self.request("/api/profile/sync", method="POST", body={})
        self.assertEqual(refreshed["profile"]["profile_name"], "Default")

        original = FakeNightscout.profile
        FakeNightscout.profile = json.loads(json.dumps(original))
        FakeNightscout.profile["store"]["Default"]["units"] = "mmol/L"
        FakeNightscout.profile["store"]["Default"]["sens"][0]["value"] = 2.8
        try:
            self.request("/api/profile/sync", method="POST", body={})
            _, settings = storage.get_settings()
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
        with patch("app.server.day_payload", return_value={"meals": []}):
            summary = self.request("/api/summary?days=1")
        self.assertEqual(summary["days"], 1)

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
        with patch("app.server.day_payload", side_effect=[{"meals": [meal]}] + [{"meals": []}] * 6):
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
        with patch("app.server.day_payload", side_effect=[{"meals": [calculated, blocked]}] +
                   [{"meals": []}] * 6):
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
        self.assertIsNotNone(storage.synced_at("2026-09-23"))
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM treatments").fetchone()[0], 2)

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
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM treatments WHERE id=?", (dose_id,)).fetchone()[0], 0)
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

        with patch("app.server.day_payload", side_effect=payload):
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
            self.assertEqual([item["deviation_mgdl"] for item in changed["calculations"]], ["0", "50"])
            self.assertEqual(storage.analysis_day_types()[monday], "free")
            self.request("/api/analysis/exclusion", method="POST", body={
                "meal_id": "work-breakfast", "day": monday, "excluded": True,
            })
            filtered = self.request("/api/summary?days=7")["groups"]["breakfast"]
            self.assertEqual(filtered["by_day_type"]["free"]["count"], 1)
            self.assertEqual(filtered["by_day_type"]["free"]["excluded_count"], 1)
            self.assertEqual(filtered["by_day_type"]["free"]["mean_units_per_ke"], "2")
            self.assertEqual([item["expected_delta_mgdl"] for item in filtered["calculations"]], ["50", "50"])

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
        with patch("app.server.day_payload", side_effect=[{"meals": meals}] + [{"meals": []}] * 13):
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

        with patch("app.server.day_payload", return_value={"meals": meals}):
            response = self.request("/api/analysis/exclusion", method="POST", body={
                "meal_id": "meal-4", "day": "2026-09-23", "excluded": True,
            })
        self.assertTrue(response["excluded"])
        self.assertIn("meal-4", storage.analysis_exclusions())
        with patch("app.server.day_payload", side_effect=[{"meals": meals}] + [{"meals": []}] * 13):
            filtered = self.request("/api/summary?days=14")["groups"]["lunch"]
        self.assertEqual(filtered["count"], 4)
        self.assertEqual(filtered["excluded_count"], 1)
        self.assertEqual(filtered["valid_count"], 5)
        self.assertEqual(float(filtered["mean_units_per_ke"]), 1.15)
        self.assertEqual(filtered["max_units_per_ke"], "1.3")
        self.assertTrue(filtered["calculations"][-1]["excluded"])
        self.assertTrue(filtered["calculations"][-1]["strong_outlier"])

        with patch("app.server.day_payload", return_value={"meals": meals}):
            self.request("/api/analysis/exclusion", method="POST", body={
                "meal_id": "meal-4", "day": "2026-09-23", "excluded": False,
            })
        self.assertNotIn("meal-4", storage.analysis_exclusions())

        with patch("app.server.day_payload", side_effect=[{"meals": meals[:4]}] + [{"meals": []}] * 13):
            small_group = self.request("/api/summary?days=14")["groups"]["lunch"]
        self.assertIsNone(small_group["q1_units_per_ke"])
        self.assertIsNone(small_group["q3_units_per_ke"])
        self.assertEqual(float(small_group["median_units_per_ke"]), 1.15)

        with patch("app.server.day_payload", side_effect=[{"meals": meals[:2]}] + [{"meals": []}] * 13):
            sparse_group = self.request("/api/summary?days=14")["groups"]["lunch"]
        self.assertEqual(sparse_group["strong_outlier_count"], 0)


if __name__ == "__main__":
    unittest.main()
