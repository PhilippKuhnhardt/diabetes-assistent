"""Single-process local web server and JSON API."""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from app import auth, calculation, dosing, episodes, glucose, nightscout, sensitivity, storage

WEB = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 64 * 1024
MEALS = {"breakfast", "lunch", "dinner"}
DOSAGE_ROLES = {"meal-dose", "correction", "snack"}


def median(values: list[Decimal]) -> Decimal:
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def quartile(values: list[Decimal], fraction: Decimal) -> Decimal:
    """Linear interpolation at positions (n - 1) × fraction in sorted values."""
    position = Decimal(len(values) - 1) * fraction
    lower = int(position)
    share = position - lower
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * share


def strong_outlier_flags(values: list[Decimal]) -> list[bool]:
    """Flag unusually distant values without claiming statistical certainty."""
    if len(values) < 3:
        return [False] * len(values)
    center = median(sorted(values))
    typical_distance = median(sorted(abs(value - center) for value in values))
    threshold = max(3 * typical_distance, center * Decimal("0.5"))
    return [abs(value - center) > threshold for value in values]


def json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    return value


def local_day_range(day: str, timezone_name: str) -> tuple[datetime, datetime]:
    parsed = date.fromisoformat(day)
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(parsed, time.min, tzinfo=zone).astimezone(timezone.utc)
    # Include the following breakfast and a short CGM interpolation buffer.
    end = datetime.combine(parsed + timedelta(days=1), time(hour=13), tzinfo=zone).astimezone(timezone.utc)
    return start - timedelta(hours=10), end


def parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Zeitpunkt benötigt eine Zeitzone")
    return parsed.astimezone(timezone.utc)


def validate_settings(value: dict) -> dict:
    _, old = storage.get_settings()
    result = dict(old)
    for key in ("timezone", "window_minutes", "peak_minutes", "duration_minutes",
                "breakfast_end_minute", "lunch_end_minute", "sport_lookback_minutes",
                "sport_effects_mgdl", "dose_target_mgdl"):
        if key in value:
            result[key] = value[key]
    # Nightscout owns the correction-factor schedule; it is not part of local settings.
    result.pop("isf_schedule", None)
    result.pop("ic_schedule", None)
    result.pop("target_schedule", None)
    result.pop("nightscout_profile", None)
    ZoneInfo(result["timezone"])
    window = int(result["window_minutes"])
    peak = int(result["peak_minutes"])
    duration = int(result["duration_minutes"])
    if not (60 <= window <= 360 and 20 <= peak < duration <= 600):
        raise ValueError("Ungültige Zeitparameter")
    breakfast_end = int(result["breakfast_end_minute"])
    lunch_end = int(result["lunch_end_minute"])
    lookback = int(result["sport_lookback_minutes"])
    dose_target = calculation.dec(result["dose_target_mgdl"])
    if not dose_target.is_finite() or not 60 <= dose_target <= 140:
        raise ValueError("Zielwert der Spritzhilfe muss zwischen 60 und 140 mg/dL liegen")
    effects = result["sport_effects_mgdl"]
    if not (0 < breakfast_end < lunch_end < 1440 and 0 <= lookback <= 1440):
        raise ValueError("Ungültige Mahlzeiten- oder Sportzeiten")
    if not isinstance(effects, dict) or set(effects) != set(episodes.SPORT_LABELS):
        raise ValueError("Ungültige Sportarten")
    clean_effects = {}
    for kind, amount in effects.items():
        effect = calculation.dec(amount)
        if not effect.is_finite() or not 0 <= effect <= 500:
            raise ValueError("Ungültiger Sporteffekt")
        clean_effects[kind] = str(effect)
    result.update({
        "window_minutes": window, "peak_minutes": peak, "duration_minutes": duration,
        "breakfast_end_minute": breakfast_end, "lunch_end_minute": lunch_end,
        "sport_lookback_minutes": lookback, "sport_effects_mgdl": clean_effects,
        "dose_target_mgdl": str(dose_target),
    })
    return result

def sync_profile_cache(force: bool = False) -> None:
    if not nightscout.is_configured():
        raise nightscout.NightscoutError("Nightscout-Zugang in Umgebungsvariablen fehlt")
    if not force and not storage.profile_sync_due():
        return
    storage.mark_profile_sync_attempt()
    profiles = nightscout.fetch_profiles()
    storage.mirror_nightscout_profiles(profiles)


def night_analysis(day: str, treatments: list[dict], entries: list[dict], meals: list[dict], settings: dict) -> dict:
    """Summarize CGM values from three hours after the last rapid dose to breakfast."""
    evening_meals = [item for item in meals if item.get("meal") == "dinner"]
    if not evening_meals:
        return {"reason": "Für diesen Tag ist kein Abendessen erfasst."}
    dinner = max(evening_meals, key=lambda item: item["at"])

    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    next_meals = episodes.meals_for_day(treatments, next_day, settings["timezone"], settings)
    breakfasts = [item for item in next_meals
                  if item.get("meal") == "breakfast" and item["at"] > dinner["at"]]
    if not breakfasts:
        return {"reason": "Für den folgenden Tag ist kein Frühstück erfasst."}
    breakfast = min(breakfasts, key=lambda item: item["at"])

    rapid_doses = [item["at"] for item in treatments
                   if dinner["at"] <= item["at"] < breakfast["at"]
                   and Decimal(str(item.get("insulin") or 0)) > 0
                   and calculation.is_rapid_insulin(item)]
    if not rapid_doses:
        return {
            "reason": "Zwischen Abendessen und Frühstück ist keine schnell wirksame Injektion erfasst.",
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
        }
    last_injection_at = max(rapid_doses)
    start = last_injection_at + timedelta(hours=3)
    end = breakfast["at"]
    if end <= start:
        return {
            "reason": "Das Frühstück beginnt vor dem Nachtstart drei Stunden nach der letzten Injektion.",
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
            "last_injection_at": last_injection_at, "start": start, "end": end,
        }
    try:
        start_glucose = calculation.interpolate_glucose(entries, start)["value"]
        end_glucose = calculation.interpolate_glucose(entries, end)["value"]
    except ValueError as error:
        return {
            "reason": str(error), "start": start, "end": end,
            "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
            "last_injection_at": last_injection_at,
        }

    candidates = [(start_glucose, start), (end_glucose, end)]
    candidates.extend(
        (calculation.dec(item["sgv"]), item["at"])
        for item in entries if start < item["at"] < end and item.get("sgv") is not None
    )
    minimum, minimum_at = min(candidates, key=lambda point: point[0])
    return {
        "dinner_at": dinner["at"], "breakfast_at": breakfast["at"],
        "last_injection_at": last_injection_at,
        "start": start, "end": end,
        "start_glucose": start_glucose, "end_glucose": end_glucose,
        "delta_mgdl": end_glucose - start_glucose,
        "minimum_glucose": minimum, "minimum_at": minimum_at,
    }


def day_payload(day: str, refresh: bool = True) -> dict:
    profile_sync_error = None
    if refresh and nightscout.is_configured():
        try:
            sync_profile_cache()
        except nightscout.NightscoutError as error:
            profile_sync_error = str(error)
    settings_id, settings = storage.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    sync_error = None
    if refresh and nightscout.is_configured():
        try:
            raw_entries, raw_treatments = nightscout.fetch_range(start, end)
            storage.mirror_day(day, raw_entries, raw_treatments, start, end)
        except nightscout.NightscoutError as error:
            sync_error = str(error)
    raw_entries, raw_treatments = storage.cached_between(start, end)
    entries = episodes.normalize_entries(raw_entries)
    treatments = episodes.normalize_treatments(raw_treatments)
    episodes.group_diary_followups(treatments, settings)
    meals = episodes.meals_for_day(treatments, day, settings["timezone"], settings)
    sport_marks = storage.analysis_sport_marks()
    rendered = []
    main_meal_ids = {item["id"] for item in treatments
                     if (item.get("role") == "meal" or item.get("eventType") == "Meal Bolus")
                     and Decimal(str(item.get("carbs") or 0)) > 0}
    shown_in_meals = set()
    for meal in meals:
        meal_settings_id, meal_settings = storage.get_settings_at(meal["at"])
        associated = [item for item in treatments if item.get("episode_id") == meal["id"] or item.get("id") == meal["source_id"]]
        output = {**meal}
        nearby_sport = [
            item for item in treatments
            if item.get("eventType") == "Exercise"
            and item["at"] + timedelta(minutes=int(item.get("duration") or 0)) <= meal["at"]
            and item["at"] + timedelta(minutes=int(item.get("duration") or 0)) >=
                meal["at"] - timedelta(minutes=int(meal_settings["sport_lookback_minutes"]))
        ]
        proposed_effect = sum((
            calculation.dec(meal_settings["sport_effects_mgdl"].get(item.get("sport_type"), 0))
            for item in nearby_sport), Decimal(0))
        mark = sport_marks.get(meal["id"], {})
        confirmed_effect = calculation.dec(mark.get("effect_mgdl", 0)) if mark.get("status") == "affected" else Decimal(0)
        output["sport"] = {
            "status": mark.get("status", "auto"),
            "suggested": bool(nearby_sport),
            "suggested_effect_mgdl": str(proposed_effect),
            "effect_mgdl": str(confirmed_effect),
            "events": [{"id": item["id"], "at": item["at"], "kind": item.get("sport_type")}
                       for item in nearby_sport],
        }
        if not meal_settings["isf_schedule"]:
            output["calculation"] = {"reason": "Korrekturfaktor-Profil aus Nightscout fehlt"}
        else:
            try:
                output["calculation"] = calculation.calculate_episode(
                    meal, treatments, entries, meal_settings, confirmed_effect)
                output["calculation"]["settings_version"] = meal_settings_id
                output["calculation"]["nightscout_profile"] = meal_settings.get("nightscout_profile")
            except ValueError as error:
                output["calculation"] = {"reason": str(error)}
        calculation_events = output["calculation"]
        included_ids = {item["id"] for key in ("carb_events", "doses")
                        for item in calculation_events.get(key, [])
                        if item.get("id") and item["id"] not in main_meal_ids}
        shown_in_meals.update(included_ids)
        event_ids = {item["id"] for item in associated}
        window_events = [item for item in treatments if item["id"] in included_ids and item["id"] not in event_ids]
        output["events"] = [episodes.serialize_treatment(item) for item in sorted(associated + window_events, key=lambda item: item["at"])]
        rendered.append(output)
    night = night_analysis(day, treatments, entries, rendered, settings)
    meal_sources = {meal["source_id"] for meal in meals}
    episode_ids = {meal["id"] for meal in meals if not meal["imported"]}
    zone = ZoneInfo(settings["timezone"])
    other_events = []
    exercises = []
    for item in treatments:
        if item["at"].astimezone(zone).date().isoformat() != day:
            continue
        if item["id"] in meal_sources or item["id"] in shown_in_meals or item.get("episode_id") in episode_ids:
            continue
        if item["eventType"] == "Exercise":
            kind = item.get("sport_type")
            note = episodes.EVENT_TAG.sub("", episodes.SPORT_TAG.sub("", item["notes"])).strip()
            for label in episodes.SPORT_LABELS.values():
                if note == label or note.startswith((label + " —", label + " |")):
                    note = note[len(label):].lstrip(" —|")
                    break
            exercises.append({
                "id": item["id"], "at": item["at"],
                "kind": kind, "label": episodes.SPORT_LABELS.get(kind, "Sportart unbekannt"),
                "duration_minutes": item["duration"], "notes": note,
            })
            continue
        other_events.append({
            "id": item["id"], "at": item["at"], "eventType": item["eventType"], "carbs": item["carbs"],
            "insulin": item["insulin"], "basal_units": item["basal_units"],
            "notes": episodes.EVENT_TAG.sub("", episodes.TAG.sub("", item["notes"])).strip(),
        })
    timeline = []
    for item in treatments:
        if item["at"].astimezone(zone).date().isoformat() != day:
            continue
        event_type = item["eventType"]
        if event_type == "Exercise":
            kind = "exercise"
        elif event_type == "Meal Bolus" and calculation.dec(item["carbs"]) > 0:
            kind = "meal"
        elif event_type == "Snack Bolus" or "[DA2:snack]" in item["notes"]:
            kind = "snack"
        elif event_type == "Carb Correction":
            kind = "carbs"
        elif event_type in {"Correction Bolus", "Meal Bolus", "Bolus"} and calculation.dec(item["insulin"]) > 0:
            kind = "correction"
        else:
            kind = "other"
        note = episodes.EVENT_TAG.sub("", episodes.SPORT_TAG.sub("",
               episodes.TAG.sub("", item["notes"]))).replace("[DA2:snack]", "").strip()
        for label in episodes.SPORT_LABELS.values():
            if kind == "exercise" and (note == label or note.startswith(label + " — ")):
                note = note[len(label):].lstrip(" —")
                break
        try:
            glucose = calculation.interpolate_glucose(entries, item["at"])["value"]
            glucose_at = item["at"]
        except ValueError:
            previous = [point for point in entries
                        if point["at"] <= item["at"]
                        and item["at"] - point["at"] <= timedelta(minutes=15)]
            glucose = calculation.dec(previous[-1]["sgv"]) if previous else None
            glucose_at = previous[-1]["at"] if previous else None
        timeline.append({
            "id": item["id"], "at": item["at"], "kind": kind, "eventType": event_type,
            "carbs": item["carbs"], "insulin": item["insulin"],
            "basal_units": item["basal_units"], "duration_minutes": item["duration"],
            "sport_kind": item.get("sport_type"), "notes": note,
            "glucose_mgdl": glucose, "glucose_at": glucose_at,
        })
    return json_safe({
        "day": day, "connected": nightscout.is_configured(),
        "synced_at": storage.synced_at(day), "sync_error": sync_error,
        "profile_sync_error": profile_sync_error,
        "settings_version": settings_id, "meals": rendered, "exercises": exercises, "other_events": other_events,
        "timeline": timeline,
        "night": night,
        "entries": [{"at": item["at"], "sgv": item["sgv"]} for item in entries],
        "treatments": [episodes.serialize_treatment(item) for item in treatments],
    })


def record_with_tag(at: datetime, event_type: str, episode_id: str, meal: str, role: str, carbs: Decimal, units: Decimal, note: str, event_id: str) -> dict:
    marker = episodes.tag(episode_id, meal, role)
    text = (note.strip() + " " + marker + f" [DAE:{event_id}]").strip()
    return {
        "eventType": event_type, "created_at": at.isoformat().replace("+00:00", "Z"),
        "carbs": float(carbs), "insulin": float(units), "notes": text,
        "enteredBy": "Diabetes Analysis PoC", "identifier": event_id,
    }


def ensure_not_duplicate(day: str, event_id: str) -> bool:
    _, settings = storage.get_settings()
    start, end = local_day_range(day, settings["timezone"])
    _, existing = nightscout.fetch_range(start, end)
    return any(item.get("identifier") == event_id or f"[DAE:{event_id}]" in str(item.get("notes") or "") for item in existing)


def save_diary_treatment(payload: dict) -> dict:
    """Validate a standalone diary event and write it to Nightscout."""
    if not nightscout.is_configured():
        raise ValueError("Zuerst Nightscout verbinden")
    kind = str(payload["kind"])
    if kind not in {"meal", "snack", "carbs", "correction", "exercise"}:
        raise ValueError("Ungültiger Eintragstyp")
    at = parse_instant(str(payload["at"]))
    _, settings = storage.get_settings()
    day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
    carbs = calculation.dec(payload.get("carbs") or 0)
    units = calculation.dec(payload.get("insulin") or 0)
    if not carbs.is_finite() or not units.is_finite() or not 0 <= carbs <= 500 or not 0 <= units <= 100:
        raise ValueError("KH oder Insulin außerhalb des erlaubten Bereichs")
    if kind in {"meal", "snack", "carbs"} and carbs <= 0:
        raise ValueError("Kohlenhydrate müssen positiv sein")
    if kind in {"correction", "exercise"} and carbs != 0:
        raise ValueError("Dieser Eintrag hat keine Kohlenhydrate")
    if ((kind in {"carbs", "exercise"} and units != 0)
            or (kind == "correction" and units <= 0)):
        raise ValueError("Ungültige Insulinmenge")
    duration = 0
    sport_kind = None
    if kind == "exercise":
        sport_kind = str(payload.get("sport_kind", ""))
        if sport_kind not in episodes.SPORT_LABELS:
            raise ValueError("Ungültige Sportart")
        duration = int(payload.get("duration_minutes", 0))
        if not 1 <= duration <= 1440:
            raise ValueError("Sportdauer muss zwischen 1 und 1440 Minuten liegen")
    note = str(payload.get("notes") or "").strip()[:500]
    record_id = str(payload.get("id") or "")
    existing = None
    if record_id:
        old_day = str(payload.get("original_day") or day)
        date.fromisoformat(old_day)
        start, end = local_day_range(old_day, settings["timezone"])
        _, records = nightscout.fetch_range(start, end)
        existing = next((item for item in records if str(item.get("_id")) == record_id), None)
        if existing is None:
            raise ValueError("Eintrag in Nightscout nicht gefunden")
        if existing.get("eventType") not in {"Meal Bolus", "Snack Bolus", "Carb Correction",
                                              "Correction Bolus", "Bolus", "Exercise"}:
            raise ValueError("Dieser Eintragstyp kann hier nicht bearbeitet werden")
    previous_id = str((existing or {}).get("identifier") or "")
    try:
        previous_id = str(uuid.UUID(previous_id))
    except ValueError:
        previous_id = ""
    event_id = previous_id or str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
    if kind == "exercise":
        event_type = "Exercise"
        label = episodes.SPORT_LABELS[sport_kind]
        notes = label + (f" — {note}" if note else "") + f" [DAS1:{sport_kind}] [DAE:{event_id}]"
    else:
        event_type = {"meal": "Meal Bolus", "snack": "Snack Bolus" if units > 0 else "Carb Correction",
                      "carbs": "Carb Correction", "correction": "Correction Bolus"}[kind]
        marker = " [DA2:snack]" if kind == "snack" else ""
        notes = (note + marker + f" [DAE:{event_id}]").strip()
        legacy = episodes.TAG.search(str((existing or {}).get("notes") or ""))
        if kind == "meal" and legacy and legacy.group(3) == "meal":
            notes += " " + legacy.group(0)
    record = dict(existing or {})
    record.update({
        "eventType": event_type, "created_at": at.isoformat().replace("+00:00", "Z"),
        "notes": notes, "enteredBy": "Diabetes Diary", "identifier": event_id,
        "carbs": float(carbs), "insulin": float(units),
    })
    if kind == "meal" and not existing:
        start, end = local_day_range(day, settings["timezone"])
        _, nearby = nightscout.fetch_range(start, end)
        classified = episodes.normalize_treatments(nearby + [record])
        episodes.group_diary_followups(classified, settings)
        candidate = next(item for item in classified if f"[DAE:{event_id}]" in item["notes"])
        if candidate["eventType"] != "Meal Bolus":
            record["eventType"] = candidate["eventType"]
            record["notes"] = (note + " [DA2:snack]" + f" [DAE:{event_id}]").strip()
    if "date" in record:
        record["date"] = int(at.timestamp() * 1000)
    if kind == "exercise":
        record["duration"] = duration
    else:
        record.pop("duration", None)
    if existing:
        nightscout.update_treatment(record_id, record)
        start, end = local_day_range(day, settings["timezone"])
        _, confirmed = nightscout.fetch_range(start, end)
        saved = next((item for item in confirmed if str(item.get("_id")) == record_id), None)
        if (saved is None or saved.get("eventType") != event_type
                or calculation.dec(saved.get("carbs") or 0) != carbs
                or calculation.dec(saved.get("insulin") or 0) != units
                or str(saved.get("notes") or "") != notes):
            raise nightscout.NightscoutError("Nightscout hat die Änderung nicht bestätigt")
        storage.delete_cached_treatment(record_id)
    elif not ensure_not_duplicate(day, event_id):
        nightscout.write_treatment(record)
    return {"id": record_id or None, "event_id": event_id, "day": day}


class Handler(BaseHTTPRequestHandler):
    server_version = "DiabetesAnalysis/0.1"

    def log_message(self, format, *args):
        # No URL logging: Nightscout credentials and patient data never enter logs.
        pass

    def _authorized(self) -> bool:
        return auth.authorized(self.headers.get("Cookie", ""))

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _response(self, status: int, payload: object):
        body = json.dumps(json_safe(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        if self._authorized():
            return True
        if urlparse(self.path).path.startswith("/api/"):
            self._response(401, {"error": "Bitte anmelden"})
        else:
            self._redirect("/login")
        return False

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("JSON-Anfrage erwartet")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("Ungültige Anfragegröße")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("JSON-Objekt erwartet")
        return value

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._response(200, {"ok": True})
            return
        if path == "/login" or path == "/styles.css":
            target = WEB / ("login.html" if path == "/login" else "styles.css")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if not self._check_auth():
            return
        try:
            if path == "/api/settings":
                profile_sync_error = None
                if nightscout.is_configured():
                    try:
                        sync_profile_cache()
                    except nightscout.NightscoutError as error:
                        profile_sync_error = str(error)
                version, settings = storage.get_settings()
                profile_sync = storage.profile_sync_status()
                self._response(200, {
                    "version": version, "settings": settings, "connected": nightscout.is_configured(),
                    "login_required": bool(auth.password()),
                    "profile_sync_at": profile_sync["synced_at"], "profile_sync_error": profile_sync_error,
                })
                return
            if path == "/api/glucose/current":
                if not nightscout.is_configured():
                    raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
                profile_current = True
                try:
                    sync_profile_cache()
                except nightscout.NightscoutError:
                    profile_current = False
                _, settings = storage.get_settings()
                profile_sync = storage.profile_sync_status()
                synced_at = profile_sync["synced_at"]
                attempted_at = profile_sync["attempted_at"]
                profile_current = (profile_current and bool(synced_at) and
                                   (not attempted_at or parse_instant(synced_at) >= parse_instant(attempted_at)))
                if not profile_current:
                    settings["target_schedule"] = []
                now = datetime.now(timezone.utc)
                raw = nightscout.fetch_recent_glucose(now)
                entries = episodes.normalize_entries(raw)
                self._response(200, glucose.current_status(entries, settings, now))
                return
            if path == "/api/settings/history":
                self._response(200, {"versions": storage.settings_history()})
                return
            if path == "/api/day":
                params = parse_qs(urlparse(self.path).query)
                day = params.get("date", [None])[0]
                if day is None:
                    raise ValueError("Datum fehlt")
                date.fromisoformat(day)
                self._response(200, day_payload(day))
                return
            if path == "/api/test-connection":
                if not nightscout.is_configured():
                    raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
                sync_profile_cache(force=True)
                _, settings = storage.get_settings()
                today = datetime.now(ZoneInfo(settings["timezone"])).date().isoformat()
                start, end = local_day_range(today, settings["timezone"])
                nightscout.fetch_range(start, end)
                self._response(200, {"message": "Nightscout-Verbindung und aktuelles Profil erfolgreich geprüft"})
                return
            if path == "/api/summary":
                params = parse_qs(urlparse(self.path).query)
                days = int(params.get("days", ["14"])[0])
                if days not in (1, 3, 7, 14, 30):
                    raise ValueError("Ungültiger Zeitraum")
                profile_sync_error = None
                if nightscout.is_configured():
                    try:
                        sync_profile_cache()
                    except nightscout.NightscoutError as error:
                        profile_sync_error = str(error)
                _, settings = storage.get_settings()
                today = datetime.now(ZoneInfo(settings["timezone"])).date()
                excluded_ids = storage.analysis_exclusions()
                day_types = storage.analysis_day_types()
                groups = {name: {"total": 0, "count": 0, "valid_count": 0, "excluded_count": 0,
                                 "blocked_count": 0, "values": [], "nightscout_values": [],
                                 "calculations": [], "multi_carb_count": 0, "shortened_count": 0}
                          for name in MEALS}
                sensitivity_meals = []
                night_records = []
                night_start_values = []
                night_end_values = []
                night_minimum_values = []
                night_delta_values = []
                for offset in range(days):
                    day = (today - timedelta(days=offset)).isoformat()
                    payload = day_payload(day, refresh=False)
                    night = payload.get("night") or {}
                    night_item = {
                        "day": day, "start": night.get("start"), "end": night.get("end"),
                        "dinner_at": night.get("dinner_at"), "breakfast_at": night.get("breakfast_at"),
                        "last_injection_at": night.get("last_injection_at"),
                        "delta_mgdl": night.get("delta_mgdl"),
                        "start_glucose": night.get("start_glucose"),
                        "end_glucose": night.get("end_glucose"),
                        "minimum_glucose": night.get("minimum_glucose"),
                        "minimum_at": night.get("minimum_at"), "reason": night.get("reason"),
                    }
                    night_records.append(night_item)
                    if all(night_item[key] is not None for key in
                           ("start_glucose", "end_glucose", "minimum_glucose")):
                        night_start_values.append(Decimal(night_item["start_glucose"]))
                        night_end_values.append(Decimal(night_item["end_glucose"]))
                        night_minimum_values.append(Decimal(night_item["minimum_glucose"]))
                        night_delta_values.append(Decimal(night_item["delta_mgdl"]))
                    day_type = day_types.get(day, "free" if date.fromisoformat(day).weekday() >= 5 else "work")
                    for meal in payload["meals"]:
                        group = groups[meal["meal"]]
                        group["total"] += 1
                        result = meal.get("calculation") or {}
                        if result.get("end_reason") == "before_next_meal":
                            group["shortened_count"] += 1
                        if result.get("extra_carb_count", 0):
                            group["multi_carb_count"] += 1
                        factor_text = result.get("factor_units_per_ke")
                        excluded = meal["id"] in excluded_ids
                        group["calculations"].append({
                            "id": meal["id"], "day": day, "at": meal["at"],
                            "day_type": day_type, "units_per_ke": factor_text,
                            "carbs_total": result.get("carbs_total"),
                            "rapid_units_in_window": result.get("rapid_units_in_window"),
                            "delta_mgdl": result.get("delta_mgdl"),
                            "end": result.get("end"),
                            "reason": result.get("reason"),
                            "excluded": excluded,
                            "sport": meal.get("sport", {}),
                        })
                        item = group["calculations"][-1]
                        item["expected_delta_mgdl"] = None
                        item["deviation_mgdl"] = None
                        if (factor_text is not None and settings["ic_schedule"]
                                and result.get("carb_events") is not None
                                and result.get("insulin_effect_mgdl") is not None
                                and result.get("delta_mgdl") is not None):
                            carb_effect = sum((
                                Decimal(event["carbs"]) * Decimal(event["isf"]) /
                                calculation.schedule_value_at(
                                    datetime.fromisoformat(event["at"]), settings["ic_schedule"],
                                    settings["timezone"], "grams_per_unit")
                                for event in result["carb_events"]), Decimal(0))
                            expected = carb_effect - Decimal(result["insulin_effect_mgdl"]) - Decimal(result.get("sport_effect_mgdl", "0"))
                            item["expected_delta_mgdl"] = str(expected)
                            item["deviation_mgdl"] = str(Decimal(result["delta_mgdl"]) - expected)
                        sensitivity_meals.append({"calculation": result, "excluded": excluded,
                                                  "deviation_mgdl": item["deviation_mgdl"]})
                        if factor_text is None:
                            group["blocked_count"] += 1
                        else:
                            group["valid_count"] += 1
                            if excluded:
                                group["excluded_count"] += 1
                            else:
                                group["count"] += 1
                                group["values"].append(Decimal(factor_text))
                                group["nightscout_values"].append(Decimal(result["ratio_g_per_unit"]))
                for group in groups.values():
                    comparable = [item for item in group["calculations"] if item["units_per_ke"] is not None]
                    for item in group["calculations"]:
                        item["strong_outlier"] = False
                    if group is groups["breakfast"]:
                        comparison_sets = [[item for item in comparable if item["day_type"] == kind]
                                           for kind in ("work", "free")]
                    else:
                        comparison_sets = [comparable]
                    for comparison in comparison_sets:
                        flags = strong_outlier_flags([Decimal(item["units_per_ke"]) for item in comparison])
                        for item, flag in zip(comparison, flags):
                            item["strong_outlier"] = flag
                    group["strong_outlier_count"] = sum(item["strong_outlier"] for item in comparable)
                    values = sorted(group.pop("values"))
                    ns_values = sorted(group.pop("nightscout_values"))
                    group["median_units_per_ke"] = str(median(values)) if values else None
                    group["mean_units_per_ke"] = str(sum(values) / len(values)) if values else None
                    group["median_g_per_unit"] = str(median(ns_values)) if ns_values else None
                    group["min_units_per_ke"] = str(values[0]) if values else None
                    group["max_units_per_ke"] = str(values[-1]) if values else None
                    # With fewer than five meals, quartiles imply more precision than the data support.
                    group["q1_units_per_ke"] = str(quartile(values, Decimal("0.25"))) if len(values) >= 5 else None
                    group["q3_units_per_ke"] = str(quartile(values, Decimal("0.75"))) if len(values) >= 5 else None
                    chronological = sorted(
                        (item for item in group["calculations"]
                         if item["units_per_ke"] is not None and not item["excluded"]),
                        key=lambda item: item["at"], reverse=True)
                    if len(chronological) >= 6:
                        recent = median(sorted(Decimal(item["units_per_ke"]) for item in chronological[:3]))
                        previous = median(sorted(Decimal(item["units_per_ke"]) for item in chronological[3:6]))
                        group["recent_median_units_per_ke"] = str(recent)
                        group["previous_median_units_per_ke"] = str(previous)
                        group["trend_units_per_ke"] = str(recent - previous)
                    else:
                        group["recent_median_units_per_ke"] = None
                        group["previous_median_units_per_ke"] = None
                        group["trend_units_per_ke"] = None
                breakfast = groups["breakfast"]
                breakfast["by_day_type"] = {}
                for kind in ("work", "free"):
                    items = [item for item in breakfast["calculations"] if item["day_type"] == kind]
                    values = sorted(Decimal(item["units_per_ke"]) for item in items
                                    if item["units_per_ke"] is not None and not item["excluded"])
                    breakfast["by_day_type"][kind] = {
                        "total": len(items), "count": len(values),
                        "valid_count": sum(item["units_per_ke"] is not None for item in items),
                        "excluded_count": sum(item["units_per_ke"] is not None and item["excluded"] for item in items),
                        "blocked_count": sum(item["units_per_ke"] is None for item in items),
                        "mean_units_per_ke": str(sum(values) / len(values)) if values else None,
                        "median_units_per_ke": str(median(values)) if values else None,
                        "min_units_per_ke": str(values[0]) if values else None,
                        "max_units_per_ke": str(values[-1]) if values else None,
                        "q1_units_per_ke": str(quartile(values, Decimal("0.25"))) if len(values) >= 5 else None,
                        "q3_units_per_ke": str(quartile(values, Decimal("0.75"))) if len(values) >= 5 else None,
                    }
                self._response(200, {
                    "days": days, "groups": groups, "profile_sync_error": profile_sync_error,
                    "sensitivity_comparison": sensitivity.compare_schedule(
                        sensitivity_meals, settings["isf_schedule"], settings["ic_schedule"],
                        settings["timezone"]),
                    "night": {
                        "count": len(night_start_values),
                        "unavailable_count": days - len(night_start_values),
                        "median_start_glucose": str(median(sorted(night_start_values))) if night_start_values else None,
                        "median_end_glucose": str(median(sorted(night_end_values))) if night_end_values else None,
                        "median_minimum_glucose": str(median(sorted(night_minimum_values))) if night_minimum_values else None,
                        "median_delta_mgdl": str(median(sorted(night_delta_values))) if night_delta_values else None,
                        "nights": night_records,
                    },
                })
                return
            target = WEB / ("index.html" if path == "/" else path.lstrip("/"))
            if not target.is_file() or not target.resolve().is_relative_to(WEB):
                self._response(404, {"error": "Nicht gefunden"})
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
        except (ValueError, KeyError) as error:
            self._response(400, {"error": str(error)})
        except nightscout.NightscoutError as error:
            self._response(502, {"error": str(error)})
        except Exception:
            self._response(500, {"error": "Interner Fehler"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                self._response(403, {"error": "Ungültiger Ursprung"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                supplied = form.get("password", [""])[0]
            except (ValueError, UnicodeDecodeError):
                supplied = ""
            if auth.password() and hmac.compare_digest(supplied, auth.password()):
                secure = self.headers.get("X-Forwarded-Proto", "") == "https"
                self._redirect("/", auth.cookie_header(secure=secure))
            elif not auth.password():
                self._redirect("/")
            else:
                self._redirect("/login?error=1")
            return
        if path == "/logout":
            secure = self.headers.get("X-Forwarded-Proto", "") == "https"
            self._redirect("/login", auth.cookie_header(secure=secure, clear=True))
            return
        if not self._check_auth():
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._response(403, {"error": "Ungültiger Ursprung"})
            return
        try:
            payload = self._body()
            if path == "/api/dose-help":
                _, current_settings = storage.get_settings()
                today = datetime.now(ZoneInfo(current_settings["timezone"])).date().isoformat()
                synced = storage.synced_at(today)
                cached_recently = bool(synced and datetime.now(timezone.utc) -
                                       parse_instant(synced) < timedelta(minutes=1))
                current_day = day_payload(today, refresh=not cached_recently)
                entries = [{"at": parse_instant(row["at"]), "sgv": row["sgv"]}
                           for row in current_day["entries"]]
                treatments = [{"at": parse_instant(row["at"]), **{k: v for k, v in row.items() if k != "at"}}
                              for row in current_day["treatments"]]
                self._response(200, dosing.suggest(payload.get("carbs", 0),
                                                   current_settings, entries, treatments))
                return
            if path == "/api/treatment":
                self._response(200, save_diary_treatment(payload))
                return
            if path == "/api/analysis/sport":
                meal_id = payload.get("meal_id")
                status = payload.get("status")
                day = str(payload.get("day", ""))
                date.fromisoformat(day)
                if not isinstance(meal_id, str) or not 0 < len(meal_id) <= 200 or status not in ("auto", "affected", "unaffected"):
                    raise ValueError("Ungültige Sport-Markierung")
                if meal_id not in {meal["id"] for meal in day_payload(day, refresh=False)["meals"]}:
                    raise ValueError("Mahlzeit nicht gefunden")
                effect = calculation.dec(payload.get("effect_mgdl", 0))
                if not effect.is_finite() or not 0 <= effect <= 500:
                    raise ValueError("Ungültiger Sporteffekt")
                storage.set_analysis_sport_mark(meal_id, status, str(effect))
                self._response(200, {"meal_id": meal_id, "status": status, "effect_mgdl": str(effect)})
                return
            if path == "/api/analysis/day-type":
                day = payload.get("day")
                day_type = payload.get("day_type")
                if not isinstance(day, str) or not isinstance(day_type, str) or day_type not in ("work", "free"):
                    raise ValueError("Ungültige Tageskennzeichnung")
                date.fromisoformat(day)
                storage.set_analysis_day_type(day, day_type)
                self._response(200, {"day": day, "day_type": day_type})
                return
            if path == "/api/analysis/exclusion":
                meal_id = payload.get("meal_id")
                excluded = payload.get("excluded")
                if not isinstance(meal_id, str) or not 0 < len(meal_id) <= 200 or not isinstance(excluded, bool):
                    raise ValueError("Ungültige Analyse-Auswahl")
                day = str(payload.get("day", ""))
                date.fromisoformat(day)
                if meal_id not in {meal["id"] for meal in day_payload(day, refresh=False)["meals"]}:
                    raise ValueError("Mahlzeit nicht gefunden")
                storage.set_analysis_exclusion(meal_id, excluded)
                self._response(200, {"meal_id": meal_id, "excluded": excluded})
                return
            if path == "/api/sync":
                if not nightscout.is_configured():
                    raise ValueError("Nightscout-Zugang in Umgebungsvariablen fehlt")
                days = int(payload.get("days", 14))
                if days not in (1, 3, 7, 14, 30):
                    raise ValueError("Ungültiger Zeitraum")
                profile_sync_error = None
                try:
                    sync_profile_cache(force=True)
                except nightscout.NightscoutError as error:
                    profile_sync_error = str(error)
                _, settings = storage.get_settings()
                today = datetime.now(ZoneInfo(settings["timezone"])).date()
                errors = []
                synced = 0
                for offset in range(days):
                    day = (today - timedelta(days=offset)).isoformat()
                    start, end = local_day_range(day, settings["timezone"])
                    try:
                        entries, treatments = nightscout.fetch_range(start, end)
                        storage.mirror_day(day, entries, treatments, start, end)
                        synced += 1
                    except nightscout.NightscoutError as error:
                        errors.append({"day": day, "error": str(error)})
                self._response(200, {"synced": synced, "errors": errors, "profile_sync_error": profile_sync_error})
                return
            if path == "/api/profile/sync":
                sync_profile_cache(force=True)
                version, settings = storage.get_settings()
                profile_sync = storage.profile_sync_status()
                profile = settings.get("nightscout_profile")
                self._response(200, {
                    "version": version, "settings": settings, "profile": profile,
                    "profile_sync_at": profile_sync["synced_at"],
                    "message": ("Nightscout-Profil synchronisiert" if profile else
                                "Nightscout hat kein Korrekturfaktor-Profil geliefert"),
                })
                return
            if path in ("/api/profile/ic", "/api/profile/isf"):
                profile_id = payload.get("profile_id")
                is_ic = path.endswith("/ic")
                schedule = payload.get("ic_schedule" if is_ic else "isf_schedule")
                if not isinstance(profile_id, str) or not profile_id or not isinstance(schedule, list):
                    raise ValueError("Ungültiger Profil-Zeitplan")
                if is_ic:
                    converted = []
                    for row in schedule:
                        if "units_per_ke" in row:
                            factor = calculation.dec(row["units_per_ke"])
                            if not factor.is_finite() or factor <= 0:
                                raise ValueError("Insulinfaktor muss positiv sein")
                            converted.append({"start_minute": row["start_minute"],
                                              "grams_per_unit": str(Decimal(10) / factor)})
                        else:
                            converted.append(row)
                    schedule = converted
                profiles = nightscout.update_profile_schedule("carbratio" if is_ic else "sens", schedule, profile_id)
                storage.mirror_nightscout_profiles(profiles)
                version, settings = storage.get_settings()
                self._response(200, {"version": version, "settings": settings,
                                     "message": ("I:C-Zeitplan" if is_ic else "Korrekturfaktor-Zeitplan") +
                                                " in Nightscout gespeichert"})
                return
            if path == "/api/treatment/delete":
                if not nightscout.is_configured():
                    raise ValueError("Zuerst Nightscout verbinden")
                record_id = payload.get("id")
                day = payload.get("day")
                if not isinstance(record_id, str) or not record_id or len(record_id) > 200 or not isinstance(day, str):
                    raise ValueError("Ungültiger Eintrag")
                date.fromisoformat(day)
                _, settings = storage.get_settings()
                start, end = local_day_range(day, settings["timezone"])
                _, treatments = nightscout.fetch_range(start, end)
                if not any(item.get("_id") == record_id for item in treatments):
                    raise ValueError("Eintrag in Nightscout nicht gefunden")
                nightscout.delete_treatment(record_id)
                storage.delete_cached_treatment(record_id)
                self._response(200, {"id": record_id})
                return
            if path == "/api/settings":
                settings = validate_settings(payload)
                effective_at = parse_instant(str(payload["effective_at"])) if payload.get("effective_at") else None
                version = storage.save_settings(settings, effective_at)
                self._response(200, {"version": version, "settings": settings})
                return
            if path == "/api/episode":
                if not nightscout.is_configured():
                    raise ValueError("Zuerst Nightscout verbinden")
                at = parse_instant(str(payload["at"]))
                meal = str(payload["meal"])
                if meal not in MEALS:
                    raise ValueError("Ungültige Mahlzeit")
                carbs = calculation.dec(payload["carbs"])
                insulin = calculation.dec(payload.get("insulin", 0))
                if not (0 < carbs <= 500 and 0 <= insulin <= 100):
                    raise ValueError("Kohlenhydrate oder Insulin außerhalb des erlaubten Bereichs")
                note = str(payload.get("notes", ""))[:500]
                episode_id = str(uuid.UUID(str(payload.get("episode_id") or uuid.uuid4())))
                event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
                _, settings = storage.get_settings()
                day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
                if not ensure_not_duplicate(day, event_id):
                    record = record_with_tag(at, "Meal Bolus", episode_id, meal, "meal", carbs, insulin, note, event_id)
                    nightscout.write_treatment(record)
                self._response(200, {"episode_id": episode_id, "event_id": event_id, "day": day})
                return
            if path == "/api/exercise":
                if not nightscout.is_configured():
                    raise ValueError("Zuerst Nightscout verbinden")
                at = parse_instant(str(payload["at"]))
                kind = str(payload["kind"])
                if kind not in episodes.SPORT_LABELS:
                    raise ValueError("Ungültige Sportart")
                duration_text = str(payload["duration_minutes"])
                if not re.fullmatch(r"\d+", duration_text):
                    raise ValueError("Dauer muss eine ganze Minutenzahl sein")
                duration = int(duration_text)
                if not 1 <= duration <= 1440:
                    raise ValueError("Dauer muss zwischen 1 und 1440 Minuten liegen")
                note = str(payload.get("notes", "")).strip()[:400]
                event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
                _, settings = storage.get_settings()
                day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
                if not ensure_not_duplicate(day, event_id):
                    label = episodes.SPORT_LABELS[kind]
                    notes = f"{label}" + (f" — {note}" if note else "")
                    notes += f" [DAS1:{kind}] [DAE:{event_id}]"
                    nightscout.write_treatment({
                        "eventType": "Exercise",
                        "created_at": at.isoformat().replace("+00:00", "Z"),
                        "duration": duration,
                        "notes": notes,
                        "enteredBy": "Diabetes Analysis PoC",
                        "identifier": event_id,
                    })
                self._response(200, {"event_id": event_id, "day": day})
                return
            if path == "/api/dose":
                if not nightscout.is_configured():
                    raise ValueError("Zuerst Nightscout verbinden")
                at = parse_instant(str(payload["at"]))
                episode_id = str(uuid.UUID(str(payload["episode_id"])))
                meal = str(payload["meal"])
                role = str(payload["role"])
                if meal not in MEALS or role not in DOSAGE_ROLES:
                    raise ValueError("Ungültiger Dosistyp")
                units = calculation.dec(payload["insulin"])
                if not 0 < units <= 100:
                    raise ValueError("Insulin außerhalb des erlaubten Bereichs")
                carbs = calculation.dec(payload.get("carbs") or 0)
                if role == "snack" and not 0 < carbs <= 500:
                    raise ValueError("Kohlenhydrate außerhalb des erlaubten Bereichs")
                if role != "snack" and carbs != 0:
                    raise ValueError("Kohlenhydrate nur für Zwischenmahlzeiten erlaubt")
                event_id = str(uuid.UUID(str(payload.get("event_id") or uuid.uuid4())))
                _, settings = storage.get_settings()
                day = at.astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
                if not ensure_not_duplicate(day, event_id):
                    nearby_days = [(at.astimezone(ZoneInfo(settings["timezone"])).date() - timedelta(days=offset)).isoformat() for offset in (0, 1)]
                    roots = [row for meal_day in nearby_days for row in day_payload(meal_day)["meals"]]
                    if not any(row["id"] == episode_id and row["meal"] == meal and 0 <= (at - parse_instant(row["at"])).total_seconds() <= 3 * 3600 for row in roots):
                        raise ValueError("Zugehörige Mahlzeit innerhalb von drei Stunden fehlt")
                    event_type = {"correction": "Correction Bolus", "meal-dose": "Meal Bolus", "snack": "Snack Bolus"}[role]
                    record = record_with_tag(at, event_type, episode_id, meal, role, carbs, units, "", event_id)
                    nightscout.write_treatment(record)
                self._response(200, {"event_id": event_id, "day": day})
                return
            self._response(404, {"error": "Nicht gefunden"})
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self._response(400, {"error": str(error)})
        except nightscout.NightscoutError as error:
            self._response(502, {"error": str(error)})
        except Exception:
            self._response(500, {"error": "Interner Fehler"})


def main():
    if not auth.password():
        raise SystemExit("APP_PASSWORD muss gesetzt sein")
    storage.init()
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8765"))
    print(f"Diabetes Analysis listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
