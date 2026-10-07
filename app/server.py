"""HTTP layer: authentication, request parsing, routing and JSON responses. No domain logic."""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import auth, nightscout
from app.store import config, db
from app.common import json_safe
from app.services import (annotations, basal, day_view, diary, dose_help, read_api, settings, summary,
                          sync)

WEB = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 64 * 1024


def day_view_request(params: dict) -> dict:
    day = params.get("date", [None])[0]
    if day is None:
        raise ValueError("Datum fehlt")
    date.fromisoformat(day)
    return day_view.day_payload(day)


GET_ROUTES = {
    "/api/settings": lambda params: settings.settings_view(),
    "/api/glucose/current": lambda params: sync.current_glucose(),
    "/api/settings/history": lambda params: {"versions": config.settings_history()},
    "/api/day": day_view_request,
    "/api/test-connection": lambda params: sync.test_connection(),
    "/api/summary": summary.summary,
}

POST_ROUTES = {
    "/api/dose-help": dose_help.dose_help,
    "/api/vacation": settings.set_vacation,
    "/api/treatment": diary.save_diary_treatment,
    "/api/analysis/sport": annotations.set_sport_mark,
    "/api/analysis/window": annotations.set_window,
    "/api/analysis/day-type": annotations.set_day_type,
    "/api/analysis/exclusion": annotations.set_exclusion,
    "/api/sync": sync.sync_days,
    "/api/profile/sync": sync.sync_profile,
    "/api/profile/ic": lambda payload: settings.save_profile_schedule("/api/profile/ic", payload),
    "/api/profile/isf": lambda payload: settings.save_profile_schedule("/api/profile/isf", payload),
    "/api/basal": basal.save_basal,
    "/api/basal/delete": basal.delete_basal_entry,
    "/api/treatment/delete": diary.delete_treatment,
    "/api/settings": settings.save_settings,
    "/api/episode": diary.save_episode,
    "/api/exercise": diary.save_exercise,
    "/api/dose": diary.save_dose,
}


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

    def _check_api_access(self) -> bool:
        """/api/v1 accepts "Authorization: Bearer <API_READ_KEY>" or the browser login."""
        header = self.headers.get("Authorization")
        if not header:
            return self._check_auth()
        if auth.api_read_authorized(header):
            return True
        self._response(401, {"error": "Ungültiger API-Schlüssel"})
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
        if path in ("/login", "/styles.css", "/logo.svg"):
            target = WEB / ("login.html" if path == "/login" else path.lstrip("/"))
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/v1" or path.startswith("/api/v1/"):
            if not self._check_api_access():
                return
            try:
                self._response(200, read_api.api_read(path, urlparse(self.path).query))
            except read_api.ApiNotFound:
                self._response(404, {"error": "Nicht gefunden"})
            except ValueError as error:
                self._response(400, {"error": str(error)})
            except Exception:
                self._response(500, {"error": "Interner Fehler"})
            return
        if not self._check_auth():
            return
        try:
            params = parse_qs(urlparse(self.path).query)
            if path in GET_ROUTES:
                self._response(200, GET_ROUTES[path](params))
                return
            if path == "/api/export":
                filename, data = read_api.export_zip(params)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
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
            if path in POST_ROUTES:
                self._response(200, POST_ROUTES[path](payload))
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
    db.init()
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8765"))
    if os.environ.get("API_READ_KEY") and not auth.read_api_key():
        print(f"API_READ_KEY ist kürzer als {auth.MIN_API_KEY_LENGTH} Zeichen; Lese-API per Schlüssel bleibt aus", flush=True)
    print(f"Diabetes Analysis listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
