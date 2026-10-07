"""Reference (golden master) tests for the architecture rework.

A fixed, synthetic 40-day dataset is loaded through a fake Nightscout and the app's own
endpoints at a frozen clock. Every response a user or script sees (diary, analysis,
dose help, export, read API) is compared with the stored snapshots in tests/golden/.
Refactoring must leave them unchanged; a deliberate change regenerates them with

    GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden

and the diff of tests/golden/ is part of the review.
"""
import hashlib
import io
import json
import math
import os
import random
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime as real_datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from app.store import db
from app.server import Handler

GOLDEN = Path(__file__).with_name("golden")
ZONE = ZoneInfo("Europe/Berlin")
FIRST_DAY = real_datetime(2026, 8, 29, tzinfo=ZONE)
DAYS = 40
# Lunch time on the last day; the dataset ends here.
NOW = real_datetime(2026, 10, 7, 12, 40, tzinfo=ZONE)


class _FrozenMeta(type):
    def __instancecheck__(cls, value):
        return isinstance(value, real_datetime)


class FrozenDatetime(real_datetime, metaclass=_FrozenMeta):
    """datetime with a settable now(); isinstance checks still accept real datetimes."""
    instant = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.astimezone().replace(tzinfo=None)


def freeze_app_clock() -> list:
    """Point every loaded app module's datetime at FrozenDatetime; return what to restore."""
    patched = []
    for name, module in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and getattr(module, "datetime", None) is real_datetime:
            module.datetime = FrozenDatetime
            patched.append(module)
    return patched


def ms(moment: real_datetime) -> int:
    return int(moment.timestamp() * 1000)


def iso_z(moment: real_datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_dataset() -> tuple[list[dict], list[dict]]:
    """Deterministic CGM readings and Nightscout treatments with the cases the analysis knows."""
    rng = random.Random(20261007)
    treatments = []
    effects = []  # (at, mg/dL, rise minutes) of carbs (+) and insulin (-) for the synthetic CGM curve

    def add(at, event_type, carbs=0, insulin=0, notes="", **extra):
        record = {"_id": f"t-{len(treatments) + 1:04d}", "eventType": event_type, "created_at": iso_z(at),
                  "carbs": carbs, "insulin": insulin, "notes": notes, "enteredBy": "Golden", **extra}
        treatments.append(record)
        if carbs:
            effects.append((at, carbs * rng.uniform(3.0, 4.2), 90))
        if insulin:
            effects.append((at, -insulin * rng.uniform(28, 40), 150))
        return record

    for offset in range(DAYS):
        day = FIRST_DAY + timedelta(days=offset)
        weekday = day.weekday()
        last = offset == DAYS - 1
        meals = [("breakfast", 7, 30), ("lunch", 12, 15), ("dinner", 18, 50)]
        if offset % 9 == 4:
            meals = meals[1:]  # no breakfast
        for kind, hour, minute in meals:
            at = day.replace(hour=hour, minute=minute) + timedelta(minutes=rng.randrange(-40, 41, 5))
            if last and kind != "breakfast":
                if kind == "lunch":
                    break
            carbs = rng.randrange(30, 115, 5)
            factor = rng.uniform(0.9, 2.1) if kind == "breakfast" else rng.uniform(0.8, 1.6)
            units = round(carbs / 10 * factor * 2) / 2
            notes = ""
            if offset % 7 == 2 and kind == "lunch":
                notes = "Pizza [DAQ:unsure] [DAQ:fat]"
            elif offset % 5 == 1:
                notes = "[DAQ:weighed]"
            if offset % 11 == 6 and kind == "dinner":
                # Sport before dinner; racing once.
                sport = "racing" if offset == 17 else "gym"
                add(at - timedelta(minutes=150), "Exercise", notes=f"{sport.title()} [DAS1:{sport}]", duration=60)
            if offset == 12 and kind == "lunch":
                add(at, "Carb Correction", carbs=carbs, notes="nur KH")
                add(at + timedelta(minutes=10), "Correction Bolus", insulin=units)
                continue
            if offset == 15 and kind == "dinner":
                # Split bolus.
                add(at, "Meal Bolus", carbs=carbs, insulin=units - 2, notes=notes)
                add(at + timedelta(minutes=25), "Correction Bolus", insulin=2)
            else:
                add(at, "Meal Bolus", carbs=carbs, insulin=units, notes=notes)
            if offset % 4 == 1 and kind == "lunch":
                add(at + timedelta(minutes=95), "Snack Bolus", carbs=rng.randrange(10, 30, 5), insulin=1)
            if offset % 6 == 3 and kind == "dinner":
                add(at + timedelta(minutes=140), "Correction Bolus", insulin=rng.choice([1, 1.5, 2]))
            if offset % 8 == 5 and kind == "breakfast":
                add(at + timedelta(minutes=60), "Carb Correction", carbs=15)
            if offset == 22 and kind == "lunch":
                # A second main meal too close to separate: the lunch factor is blocked.
                add(at + timedelta(minutes=70), "Meal Bolus", carbs=40, insulin=4)
        if offset % 10 == 8:
            add(day.replace(hour=7, minute=5), "Note", notes="Basal 17 IE [DA2:basal] "
                f"[DAE:00000000-0000-4000-8000-{offset:012d}]", basal_units=17,
                identifier=f"00000000-0000-4000-8000-{offset:012d}")
        if offset == 30:
            add(day.replace(hour=15), "Temp Basal", notes="ignored", duration=30)

    entries = []
    start = FIRST_DAY.replace(hour=0)
    gaps = [  # missing CGM readings (sensor change, dropouts)
        (FIRST_DAY + timedelta(days=9, hours=10), FIRST_DAY + timedelta(days=9, hours=11)),
        (FIRST_DAY + timedelta(days=26, hours=14), FIRST_DAY + timedelta(days=26, hours=15, minutes=40)),
    ]
    moment = start
    index = 0
    while moment <= NOW:
        if not any(lo <= moment < hi for lo, hi in gaps):
            value = 112 + 18 * math.sin(moment.timestamp() / 86400 * 2 * math.pi)
            for at, size, rise in effects:
                minutes = (moment - at).total_seconds() / 60
                if 0 < minutes < 480:
                    shape = min(1, minutes / rise) if minutes < 240 else max(0, (480 - minutes) / 240)
                    value += size * shape
            value += rng.uniform(-6, 6)
            sgv = int(max(40, min(400, round(value))))
            entries.append({"_id": f"e-{index:05d}", "date": ms(moment) + rng.randrange(0, 20000),
                            "type": "sgv", "sgv": sgv})
            index += 1
        moment += timedelta(minutes=5)
    return entries, treatments


PROFILE = {
    "_id": "golden-profile", "startDate": "2026-01-01T00:00:00.000Z", "defaultProfile": "Default",
    "store": {"Default": {
        "timezone": "Europe/Berlin", "units": "mg/dl",
        "sens": [{"time": "00:00", "timeAsSeconds": 0, "value": 30},
                 {"time": "11:00", "timeAsSeconds": 39600, "value": 35},
                 {"time": "19:00", "timeAsSeconds": 68400, "value": 35}],
        "carbratio": [{"time": "00:00", "timeAsSeconds": 0, "value": 6},
                      {"time": "11:00", "timeAsSeconds": 39600, "value": 8},
                      {"time": "17:00", "timeAsSeconds": 61200, "value": 7.5}],
        "target_low": [{"time": "00:00", "value": 80}],
        "target_high": [{"time": "00:00", "value": 180}],
    }},
}


class GoldenNightscout(BaseHTTPRequestHandler):
    entries: list = []
    treatments: list = []
    profile: dict = {}
    counter = 0

    def log_message(self, *args):
        pass

    def _send(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        if parsed.path.endswith("/entries/sgv.json"):
            lo = int(query.get("find[date][$gte]", 0))
            hi = int(query.get("find[date][$lt]", 2 ** 62))
            self._send(sorted((item for item in self.entries if lo <= item["date"] < hi),
                              key=lambda item: -item["date"]))
        elif parsed.path.endswith("/treatments.json"):
            lo = query.get("find[created_at][$gte]", "")
            hi = query.get("find[created_at][$lt]", "9999")
            parse = lambda text: real_datetime.fromisoformat(text.replace("Z", "+00:00"))
            self._send([item for item in self.treatments
                        if parse(lo) <= parse(item["created_at"]) < parse(hi)] if lo else list(self.treatments))
        elif parsed.path.endswith("/profile/current"):
            self._send(self.profile)
        else:
            self.send_error(404)

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for item in value:
            type(self).counter += 1
            item["_id"] = f"ns-{type(self).counter:04d}"
            self.treatments.append(item)
        self._send(value)

    def do_PUT(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for index, existing in enumerate(self.treatments):
            if existing.get("_id") == value.get("_id"):
                self.treatments[index] = value
                self._send(value)
                return
        self.send_error(404)

    def do_DELETE(self):
        record_id = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
        self.treatments[:] = [item for item in self.treatments if item.get("_id") != record_id]
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GoldenMasterTests(unittest.TestCase):
    """One scenario: load the dataset, add local decisions, then record every view."""

    @classmethod
    def setUpClass(cls):
        cls.old_env = dict(os.environ)
        cls.old_db = db.DB_PATH
        cls.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.temp.name) / "app.sqlite3"
        cls.patched = freeze_app_clock()
        FrozenDatetime.instant = FIRST_DAY - timedelta(days=1)
        db.init()
        entries, treatments = build_dataset()
        GoldenNightscout.entries = entries
        GoldenNightscout.treatments = treatments
        GoldenNightscout.profile = json.loads(json.dumps(PROFILE))
        GoldenNightscout.counter = 0
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), GoldenNightscout)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        os.environ.update({
            "NIGHTSCOUT_URL": f"http://127.0.0.1:{cls.fake.server_port}",
            "NIGHTSCOUT_AUTH_MODE": "token", "NIGHTSCOUT_CREDENTIAL": "golden-token",
            "APP_PASSWORD": "", "API_READ_KEY": "",
        })
        cls.app = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.app.server_port}"
        cls.results = {}
        cls.scenario()

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.fake.shutdown()
        cls.app.server_close()
        cls.fake.server_close()
        for module in cls.patched:
            module.datetime = real_datetime
        db.DB_PATH = cls.old_db
        os.environ.clear()
        os.environ.update(cls.old_env)
        cls.temp.cleanup()

    @classmethod
    def call(cls, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(cls.base + path, data=data, method="POST" if body is not None else "GET",
                                         headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request) as response:
                content = response.read()
                return content if raw else json.loads(content)
        except urllib.error.HTTPError as error:
            payload = {"status": error.code, "body": json.loads(error.read() or b"null")}
            error.close()
            return payload

    @staticmethod
    def compact_day(payload):
        """The raw CGM echo is large and plain mirror data: keep its count and digest."""
        entries = payload.get("entries", [])
        echo = [{"at": item["at"], "sgv": item["sgv"]} for item in entries]
        tones = {}
        for item in entries:
            tones[item.get("tone")] = tones.get(item.get("tone"), 0) + 1
        return {**payload, "entries": {"count": len(entries), "tones": tones,
                                       "sha256": digest(json.dumps(echo, sort_keys=True).encode())}}

    @classmethod
    def at(cls, moment):
        FrozenDatetime.instant = moment

    @classmethod
    def scenario(cls):
        record = cls.results
        days = [(FIRST_DAY + timedelta(days=offset)).date().isoformat() for offset in range(DAYS)]
        # Settings before the data starts (first version applies to all meals), then a later version.
        record["settings_initial_save"] = cls.call("/api/settings", {
            "dose_target_mgdl": "120", "dose_cautious_target_mgdl": "150", "auto_percentile": "37.5",
            "auto_half_life_days": "5", "auto_lookback_days": 15, "window_minutes": 180,
            "sport_lookback_minutes": 360,
        })
        cls.at(FIRST_DAY + timedelta(days=20, hours=3))
        record["settings_mid_save"] = cls.call("/api/settings", {"auto_lookback_days": 30, "window_minutes": 170})
        # Mirror every day from Nightscout at the frozen "now".
        cls.at(NOW)
        loaded = {day: cls.call("/api/day?date=" + day) for day in days}

        def meal_id(day, kind):
            return next(meal["id"] for meal in loaded[day]["meals"] if meal["meal"] == kind)

        # Local decisions, as the diary and analysis pages store them.
        record["post_exclusion"] = cls.call("/api/analysis/exclusion", {
            "meal_id": meal_id(days[3], "lunch"), "day": days[3], "excluded": True})
        sport_day = days[6]
        record["post_sport"] = cls.call("/api/analysis/sport", {
            "meal_id": meal_id(sport_day, "dinner"), "day": sport_day, "status": "affected", "effect_mgdl": "35"})
        record["post_sport_racing"] = cls.call("/api/analysis/sport", {
            "meal_id": meal_id(days[17], "dinner"), "day": days[17], "status": "affected", "effect_mgdl": "-40"})
        record["post_sport_unaffected"] = cls.call("/api/analysis/sport", {
            "meal_id": meal_id(days[28], "dinner"), "day": days[28], "status": "unaffected"})
        window_day = days[10]
        window_meal = next(meal for meal in loaded[window_day]["meals"] if meal["meal"] == "dinner")
        meal_at = real_datetime.fromisoformat(window_meal["at"])
        record["post_window"] = cls.call("/api/analysis/window", {
            "meal_id": window_meal["id"], "day": window_day,
            "start": (meal_at - timedelta(minutes=20)).isoformat(),
            "end": (meal_at + timedelta(minutes=200)).isoformat()})
        record["post_window_invalid"] = cls.call("/api/analysis/window", {
            "meal_id": window_meal["id"], "day": window_day,
            "start": (meal_at - timedelta(minutes=200)).isoformat(),
            "end": (meal_at + timedelta(minutes=60)).isoformat()})
        blocked_meal = meal_id(days[22], "lunch")
        record["post_window_blocked"] = cls.call("/api/analysis/window", {
            "meal_id": blocked_meal, "day": days[22],
            "start": loaded[days[22]]["meals"][1]["at"],
            "end": (real_datetime.fromisoformat(loaded[days[22]]["meals"][1]["at"]) + timedelta(minutes=60)).isoformat()})
        record["post_day_type_free"] = cls.call("/api/analysis/day-type", {"day": days[24], "day_type": "free"})
        record["post_day_type_work"] = cls.call("/api/analysis/day-type", {"day": days[29], "day_type": "work"})
        cls.at(FIRST_DAY + timedelta(days=33, hours=8))
        record["post_vacation_on"] = cls.call("/api/vacation", {"active": True})
        cls.at(FIRST_DAY + timedelta(days=35, hours=20))
        record["post_vacation_off"] = cls.call("/api/vacation", {"active": False})
        # Diary entries through the app on the last day (snack chip, own main meal, quality marks).
        cls.at(NOW.replace(hour=12, minute=10))
        record["post_basal"] = cls.call("/api/basal", {
            "at": NOW.replace(hour=7, minute=10).isoformat(), "units": "17",
            "event_id": "11111111-1111-4111-8111-111111111111"})
        record["post_meal"] = cls.call("/api/treatment", {
            "kind": "meal", "at": NOW.replace(hour=12, minute=5).isoformat(), "carbs": "70", "insulin": "7",
            "notes": "Mittag", "carbs_certainty": "known", "fat_protein": True,
            "event_id": "22222222-2222-4222-8222-222222222222"})
        record["post_snack_auto"] = cls.call("/api/treatment", {
            "kind": "meal", "at": NOW.replace(hour=12, minute=35).isoformat(), "carbs": "15", "insulin": "0",
            "event_id": "33333333-3333-4333-8333-333333333333"})
        record["post_exercise"] = cls.call("/api/treatment", {
            "kind": "exercise", "at": NOW.replace(hour=9).isoformat(), "sport_kind": "jogging",
            "duration_minutes": 40, "notes": "Runde", "event_id": "44444444-4444-4444-8444-444444444444"})
        record["post_invalid_treatment"] = cls.call("/api/treatment", {
            "kind": "meal", "at": NOW.isoformat(), "carbs": "600"})

        # Views at lunch time on the last day.
        cls.at(NOW)
        record["settings"] = cls.call("/api/settings")
        record["glucose_current"] = cls.call("/api/glucose/current")
        record["days"] = {day: cls.compact_day(cls.call("/api/day?date=" + day)) for day in days}
        record["day_after"] = cls.call("/api/day?date=" + (NOW + timedelta(days=1)).date().isoformat())
        for span in (1, 7, 30):
            record[f"summary_{span}"] = cls.call(f"/api/summary?days={span}")
        record["dose_help"] = {
            "lunch_60g": cls.call("/api/dose-help", {"carbs": "60"}),
            "lunch_snack": cls.call("/api/dose-help", {"carbs": "20", "snack": True}),
            "correction_only": cls.call("/api/dose-help", {"carbs": "0"}),
            "sport": cls.call("/api/dose-help", {"carbs": "45", "activity_change_mgdl": -30}),
            "invalid": cls.call("/api/dose-help", {"carbs": "900"}),
        }
        record["v1"] = {name: cls.call(f"/api/v1/{name}?days=45")
                        for name in ("meals", "window-events", "basal", "profile")}
        record["v1"]["index"] = cls.call("/api/v1")
        record["v1"]["range"] = cls.call(f"/api/v1/meals?from={days[5]}&to={days[9]}")
        record["v1"]["bad_range"] = cls.call("/api/v1/meals?days=0")
        raw_tables = cls.call("/api/v1/data?days=45")
        record["v1"]["data_tables"] = {name: {"rows": len(rows), "sha256": digest(json.dumps(rows, sort_keys=True).encode())}
                                       for name, rows in raw_tables["tables"].items()}
        archive = zipfile.ZipFile(io.BytesIO(cls.call("/api/export?days=45", raw=True)))
        record["export"] = {name: digest(archive.read(name)) for name in sorted(archive.namelist())}
        record["export_meals_csv"] = archive.read("meals.csv").decode()
        # The same analysis with the surcharge method, then dinner and next-morning dose help.
        record["settings_surcharge_save"] = cls.call("/api/settings", {"auto_method": "surcharge"})
        record["summary_30_surcharge"] = cls.call("/api/summary?days=30")
        record["dose_help"]["lunch_surcharge"] = cls.call("/api/dose-help", {"carbs": "60"})
        record["dose_help"]["lunch_surcharge_snack"] = cls.call("/api/dose-help", {"carbs": "20", "snack": True})
        cls.at(NOW.replace(hour=19, minute=30))
        record["dose_help"]["dinner_stale_cgm"] = cls.call("/api/dose-help", {"carbs": "50"})
        cls.at(NOW.replace(hour=7, minute=45) + timedelta(days=1))
        record["dose_help"]["next_breakfast"] = cls.call("/api/dose-help", {"carbs": "40"})

    def check(self, name):
        value = json.loads(json.dumps(self.results[name], sort_keys=True))
        path = GOLDEN / f"{name}.json"
        if os.environ.get("GOLDEN_UPDATE"):
            GOLDEN.mkdir(exist_ok=True)
            path.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        self.assertTrue(path.exists(), f"{path} fehlt: GOLDEN_UPDATE=1 setzen")
        self.assertEqual(json.loads(path.read_text()), value, f"{name} weicht vom Referenzstand ab")

    def test_posts_and_settings(self):
        for name in ("settings_initial_save", "settings_mid_save", "post_exclusion", "post_sport",
                     "post_sport_racing", "post_sport_unaffected", "post_window", "post_window_invalid",
                     "post_window_blocked", "post_day_type_free", "post_day_type_work", "post_vacation_on",
                     "post_vacation_off", "post_basal", "post_meal", "post_snack_auto", "post_exercise",
                     "post_invalid_treatment", "settings", "glucose_current", "settings_surcharge_save"):
            with self.subTest(name):
                self.check(name)

    def test_diary_days(self):
        self.check("days")
        self.check("day_after")

    def test_analysis_summary(self):
        for name in ("summary_1", "summary_7", "summary_30", "summary_30_surcharge"):
            with self.subTest(name):
                self.check(name)

    def test_dose_help(self):
        self.check("dose_help")

    def test_read_api_and_export(self):
        self.check("v1")
        self.check("export")
        self.check("export_meals_csv")

    def test_dataset_covers_the_cases(self):
        """Guards the fixture itself: blocked, excluded, manual, sport and snack cases exist."""
        meals = [meal for day in self.results["days"].values() for meal in day["meals"]]
        reasons = {(meal.get("calculation") or {}).get("reason") for meal in meals} - {None}
        self.assertGreaterEqual(len(meals), 100)
        self.assertGreaterEqual(len(reasons), 2, reasons)
        self.assertTrue(any((meal.get("calculation") or {}).get("manual_window") for meal in meals))
        self.assertTrue(any(meal["sport"]["status"] == "affected" for meal in meals))
        self.assertTrue(any((meal.get("calculation") or {}).get("extra_carb_count") for meal in meals))
        cells = self.results["summary_30"]["suggestion_derivation"]["variants"]
        self.assertTrue(any(cell["stats"]["glucose_count"] for row in cells for cell in row["cells"]))
        self.assertIn("auto_entry_units", self.results["dose_help"]["lunch_60g"])


if __name__ == "__main__":
    unittest.main()
