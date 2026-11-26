import sys, os, json, math, tempfile, threading, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone, timedelta
from pathlib import Path
from app.store import db
db.DB_PATH = Path(tempfile.mkdtemp()) / "app.sqlite3"
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
entries = []
t = now - timedelta(days=3)
i = 0
while t <= now:
    h = t.hour + t.minute/60
    v = 120 + 40*math.sin(h/3) + 25*math.sin(h/1.3)
    entries.append({"_id": f"g{i}", "date": int(t.timestamp()*1000), "type": "sgv", "sgv": int(v), "direction": "Flat"})
    t += timedelta(minutes=5); i += 1
entries.reverse()
# The fake days are days in Berlin, like the app and the test count them: built in UTC, they slid a day
# against the test's "yesterday" between midnight in Berlin and midnight UTC (flaky runs 2026-11-26).
from zoneinfo import ZoneInfo
BERLIN = ZoneInfo("Europe/Berlin")
berlin_midnight = now.astimezone(BERLIN).replace(hour=0, minute=0)
def local(days_ago, hours, minutes=0):
    """A wall-clock time in Berlin `days_ago` days before today, in UTC."""
    day = (berlin_midnight - timedelta(days=days_ago)).replace(tzinfo=None)
    return (day + timedelta(hours=hours, minutes=minutes)).replace(tzinfo=BERLIN).astimezone(timezone.utc)
# A sensor gap over lunch two days ago: that meal's factor cannot be calculated, so "Zu erledigen"
# always has a hint to open, whatever the hour the suite runs (CI run 2026-11-19 02:30 UTC had none).
gap = (int(local(2, 11, 30).timestamp() * 1000), int(local(2, 16).timestamp() * 1000))
entries = [entry for entry in entries if not gap[0] <= entry["date"] <= gap[1]]
treat = []
def add(dt, **kw):
    kw.update({"_id": f"t{len(treat)}", "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "eventType": "Meal Bolus"})
    treat.append(kw)
for d in range(3, -1, -1):
    for hh, c, ins in ((7, 45, 3.5), (12, 70, 4.5), (18, 78, 4.5)):
        dt = local(d, hh, 20)
        if dt < now: add(dt, carbs=c, insulin=ins, notes="")
    dt = local(d, 15)
    if dt < now: add(dt, carbs=15, notes="")
# A run yesterday afternoon: its effect reaches into lunch and dinner, so Verlauf shows a sport card and shares.
run = local(1, 16)
treat.append({"_id": f"t{len(treat)}", "created_at": run.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "eventType": "Exercise",
              "duration": 45, "notes": "Joggen [DAS1:jogging]"})
# "Heute" always has a meal, whatever the hour the suite runs: before the first fixed meal (07:20)
# today would be empty and the smoke test of today's meals fails (CI run 2026-11-19 02:30 UTC).
berlin_midnight = berlin_midnight.astimezone(timezone.utc)
if not any(t["created_at"] >= berlin_midnight.strftime("%Y-%m-%dT%H:%M:%S.000Z") and t.get("insulin") for t in treat):
    add(max(now - timedelta(minutes=40), berlin_midnight + timedelta(minutes=1)), carbs=30, insulin=2.5, notes="")
PROFILE = {"_id": "p", "startDate": "2026-01-01T00:00:00.000Z", "defaultProfile": "Default", "store": {"Default": {
  "timezone": "Europe/Berlin", "units": "mg/dl",
  "sens": [{"time": "00:00", "timeAsSeconds": 0, "value": 40}],
  "carbratio": [{"time": "00:00", "timeAsSeconds": 0, "value": 8}],
  "target_low": [{"time": "00:00", "timeAsSeconds": 0, "value": 100}],
  "target_high": [{"time": "00:00", "timeAsSeconds": 0, "value": 120}]}}}
class NS(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, v):
        raw = json.dumps(v).encode(); self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p.endswith("/entries/sgv.json") or p.endswith("/entries.json"): return self._send(entries)
        if p.endswith("/treatments.json"): return self._send(treat)
        if "profile" in p: return self._send(PROFILE if p.endswith("current") else [PROFILE])
        if "status" in p: return self._send({"status": "ok"})
        self.send_error(404)
    def do_POST(self):
        v = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for it in (v if isinstance(v, list) else [v]):
            it["_id"] = f"t{len(treat)}"; treat.append(it)
        self._send(v)
    def do_DELETE(self):
        tid = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
        treat[:] = [e for e in treat if e["_id"] != tid]
        self._send({"ok": 1})
    def do_PUT(self):
        v = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for k, e in enumerate(treat):
            if e["_id"] == v.get("_id"): treat[k] = v
        self._send(v)
fake = ThreadingHTTPServer(("127.0.0.1", 8799), NS)
threading.Thread(target=fake.serve_forever, daemon=True).start()
os.environ.update({"NIGHTSCOUT_URL": "http://127.0.0.1:8799", "NIGHTSCOUT_AUTH_MODE": "token", "NIGHTSCOUT_CREDENTIAL": "x", "APP_PASSWORD": "", "OPENROUTER_API_KEY": "dummy"})
db.init()
# The default basal is 0 (off); the suite checks that the usual dose rides along, so it sets one.
from app.store import config
config.save_settings({**config.default_settings(), "basal_default_units": "17"})
from app.server import Handler
from app.services import day_view
# The red dot on "Verlauf" counts from a fixed day in production; here the fake days of the last week count.
day_view.REVIEW_SINCE = (datetime.now(ZoneInfo("Europe/Berlin")).date() - timedelta(days=7)).isoformat()
ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
