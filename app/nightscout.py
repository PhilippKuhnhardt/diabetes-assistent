"""Small Nightscout v1 adapter. Credentials come from the server's .env."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class NightscoutError(Exception):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("NIGHTSCOUT_URL", "").strip() and os.environ.get("NIGHTSCOUT_CREDENTIAL", "").strip())


def _request(method: str, path: str, query: dict | None = None, body: object | None = None):
    base = os.environ.get("NIGHTSCOUT_URL", "").strip().rstrip("/")
    credential = os.environ.get("NIGHTSCOUT_CREDENTIAL", "").strip()
    if not base or not credential:
        raise NightscoutError("Nightscout-Zugang in .env fehlt")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise NightscoutError("Nightscout muss per HTTPS erreichbar sein")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NightscoutError("Ungültige Nightscout-URL")
    params = dict(query or {})
    mode = os.environ.get("NIGHTSCOUT_AUTH_MODE", "secret")
    headers = {"Accept": "application/json", "User-Agent": "DiabetesAnalysisPoC/1"}
    if mode == "secret":
        headers["api-secret"] = hashlib.sha1(credential.encode("utf-8")).hexdigest()
    elif mode == "token":
        params["token"] = credential
    else:
        raise NightscoutError("Unbekannte Nightscout-Anmeldung")
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise NightscoutError("Nightscout lehnt die Anmeldung ab") from None
        raise NightscoutError(f"Nightscout antwortet mit HTTP {error.code}") from None
    except urllib.error.URLError:
        raise NightscoutError("Nightscout ist nicht erreichbar") from None
    except (ValueError, json.JSONDecodeError):
        raise NightscoutError("Ungültige Antwort von Nightscout") from None


def fetch_range(start: datetime, end: datetime) -> tuple[list[dict], list[dict]]:
    entries = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(hours=8), end)
        start_ms = int(cursor.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        chunk = _request("GET", "/api/v1/entries/sgv.json", {
            "find[date][$gte]": start_ms,
            "find[date][$lt]": end_ms,
            "count": 1000,
        })
        if not isinstance(chunk, list):
            raise NightscoutError("Nightscout lieferte unerwartete CGM-Daten")
        if len(chunk) >= 1000:
            raise NightscoutError("Zu viele CGM-Werte in einem 8-Stunden-Abschnitt")
        entries.extend(chunk)
        cursor = chunk_end
    treatments = _request("GET", "/api/v1/treatments.json", {
        "find[created_at][$gte]": start.isoformat().replace("+00:00", "Z"),
        "find[created_at][$lt]": end.isoformat().replace("+00:00", "Z"),
        "count": 1000,
    })
    if not isinstance(entries, list) or not isinstance(treatments, list):
        raise NightscoutError("Nightscout lieferte unerwartete Daten")
    if len(treatments) >= 1000:
        raise NightscoutError("Zu viele Nightscout-Behandlungen für eine Abfrage")
    return entries, treatments


def fetch_profiles() -> list[dict]:
    """Read only Nightscout's current profile and normalize its ISF schedule."""
    try:
        record = _request("GET", "/api/v1/profile/current")
    except NightscoutError as error:
        # Older Nightscout releases may not expose /profile/current.
        if "HTTP 404" not in str(error):
            raise
        records = _request("GET", "/api/v1/profile.json", {"count": 1})
        if not isinstance(records, list):
            raise NightscoutError("Nightscout lieferte ein ungültiges Profil") from None
        record = records[0] if records else None
    if record is None:
        return []
    if isinstance(record, list):
        record = record[0] if record else None
        if record is None:
            return []
    try:
        start = datetime.fromisoformat(str(record["startDate"]).replace("Z", "+00:00"))
        if start.tzinfo is None:
            raise ValueError
        start_at = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        profile_name = str(record["defaultProfile"])
        profile = record["store"][profile_name]
        profile_timezone = str(profile["timezone"])
        ZoneInfo(profile_timezone)
        units = str(profile.get("units", "mg/dl")).strip().lower().replace(" ", "")
        if units in ("mmol", "mmol/l", "mmoll"):
            conversion = Decimal("18.0182")
        elif units in ("mg/dl", "mgdl"):
            conversion = Decimal(1)
        else:
            raise ValueError
        source_schedule = profile["sens"]
        if not isinstance(source_schedule, list) or not source_schedule or len(source_schedule) > 48:
            raise ValueError
        schedule = []
        seen = set()
        for row in source_schedule:
            time_text = row.get("time")
            if isinstance(time_text, str) and len(time_text) == 5 and time_text[2:3] == ":":
                hour, minute = int(time_text[:2]), int(time_text[3:5])
            else:
                seconds = int(row["timeAsSeconds"])
                if not 0 <= seconds < 86400 or seconds % 60:
                    raise ValueError
                hour, minute = divmod(seconds // 60, 60)
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
            start_minute = hour * 60 + minute
            value = Decimal(str(row["value"])) * conversion
            if not (0 <= start_minute < 1440 and 0 < value <= 1000) or start_minute in seen:
                raise ValueError
            seen.add(start_minute)
            schedule.append({"start_minute": start_minute, "mgdl_per_unit": str(value)})
        if 0 not in seen:
            raise ValueError
        schedule.sort(key=lambda item: item["start_minute"])
        return [{
            "start_at": start_at,
            "profile_id": str(record.get("_id", "")),
            "profile_name": profile_name,
            "timezone": profile_timezone,
            "isf_schedule": schedule,
        }]
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation, ZoneInfoNotFoundError):
        raise NightscoutError("Nightscout lieferte ein ungültiges Korrekturfaktor-Profil") from None

def write_treatment(record: dict) -> None:
    response = _request("POST", "/api/v1/treatments.json", body=[record])
    # Nightscout versions differ in the POST response. Verify a nonempty response
    # against the authoritative treatments endpoint before calling it a rejection.
    if isinstance(response, list) and response:
        at = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        found = _request("GET", "/api/v1/treatments.json", {
            "find[created_at][$gte]": (at - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "find[created_at][$lt]": (at + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "count": 100,
        })
        identifier = record.get("identifier")
        if not isinstance(found, list) or not any(
            item.get("identifier") == identifier or f"[DAE:{identifier}]" in str(item.get("notes") or "")
            for item in found
        ):
            raise NightscoutError("Nightscout hat den Eintrag nicht bestätigt")


def delete_treatment(record_id: str) -> None:
    safe_id = urllib.parse.quote(record_id, safe="")
    _request("DELETE", f"/api/v1/treatments/{safe_id}")
