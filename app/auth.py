"""Password login: browser sessions that logout and a password change end, and a brake on wrong passwords."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

from app.store.db import connect

COOKIE_NAME = "diary_session"
# The MCP key (write access for an agent) has its own variable and never reuses the read key.
MIN_API_KEY_LENGTH = 32
MAX_AGE = 365 * 24 * 3600


def password() -> str:
    return os.environ.get("APP_PASSWORD", "")


def _password_tag() -> str:
    """Stored with each session: a new APP_PASSWORD ends every session made with the old one."""
    return hmac.new(password().encode(), b"diabetes-diary-session-v2", hashlib.sha256).hexdigest()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _session_value(cookie_header: str) -> str:
    try:
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        return cookies[COOKIE_NAME].value
    except (KeyError, ValueError):
        return ""


def new_session() -> str:
    """A random session value; only its hash is stored, so a copy of the database opens no session."""
    value = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with connect() as db:
        db.execute("DELETE FROM browser_sessions WHERE expires_at < ?", (now.isoformat(),))
        db.execute("INSERT INTO browser_sessions(token_hash, password_tag, created_at, expires_at) VALUES(?,?,?,?)",
                   (_hash(value), _password_tag(), now.isoformat(), (now + timedelta(seconds=MAX_AGE)).isoformat()))
    return value


def authorized(cookie_header: str) -> bool:
    if not password():
        return True
    value = _session_value(cookie_header)
    if not value:
        return False
    with connect() as db:
        row = db.execute("SELECT password_tag, expires_at FROM browser_sessions WHERE token_hash=?",
                         (_hash(value),)).fetchone()
    return (row is not None and hmac.compare_digest(row["password_tag"], _password_tag())
            and row["expires_at"] > datetime.now(timezone.utc).isoformat())


def end_session(cookie_header: str) -> None:
    """Logout: the session stops working on the server too, not only in this browser."""
    value = _session_value(cookie_header)
    if value:
        with connect() as db:
            db.execute("DELETE FROM browser_sessions WHERE token_hash=?", (_hash(value),))


def cookie_header(value: str = "", secure: bool = False, clear: bool = False) -> str:
    header = f"{COOKIE_NAME}={'' if clear else value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={0 if clear else MAX_AGE}"
    return header + ("; Secure" if secure else "")


# Wrong passwords (login page and agent connect page). Per client address, plus a ceiling for all
# addresses together so changing addresses does not help; a signed-in browser keeps working meanwhile.
FAILURE_WINDOW = 15 * 60
CLIENT_FAILURES = 5
TOTAL_FAILURES = 20
_failures: dict[str, deque] = {}
_failures_lock = threading.Lock()
_ALL = "*"


def client_address(headers, peer: str) -> str:
    """The caller's address. Behind the proxy the last X-Forwarded-For entry is the one the proxy added;
    earlier entries come from the caller and could be anything."""
    forwarded = [item.strip() for item in headers.get("X-Forwarded-For", "").split(",") if item.strip()]
    return forwarded[-1] if forwarded else peer


def _recent(key: str, now: float) -> deque:
    times = _failures.setdefault(key, deque())
    while times and times[0] <= now - FAILURE_WINDOW:
        times.popleft()
    return times


def wait_seconds(client: str) -> int:
    """0 when a password may be tried now, else the seconds until the next try."""
    now = time.monotonic()
    with _failures_lock:
        waits = [times[0] + FAILURE_WINDOW - now for key, limit in ((client, CLIENT_FAILURES), (_ALL, TOTAL_FAILURES))
                 if len(times := _recent(key, now)) >= limit]
    return max(1, int(max(waits)) + 1) if waits else 0


def record_failure(client: str) -> None:
    now = time.monotonic()
    with _failures_lock:
        for key in (client, _ALL):
            _recent(key, now).append(now)
        for key in [key for key, times in _failures.items() if not times]:
            del _failures[key]


def record_success(client: str) -> None:
    with _failures_lock:
        _failures.pop(client, None)


def reset_failures() -> None:
    with _failures_lock:
        _failures.clear()


def check_password(supplied: str, client: str) -> str:
    """"ok", "wrong" or "wait" (too many wrong tries; the password is not even compared then)."""
    if wait_seconds(client):
        return "wait"
    expected = password()
    if expected and hmac.compare_digest(supplied.encode(), expected.encode()):
        record_success(client)
        return "ok"
    record_failure(client)
    return "wrong"


def read_api_key() -> str:
    """Read-only key for scripts and agents (`API_READ_KEY`); too short or unset disables key access."""
    key = os.environ.get("API_READ_KEY", "")
    return key if len(key) >= MIN_API_KEY_LENGTH else ""


def api_read_authorized(authorization_header: str) -> bool:
    """True for "Authorization: Bearer <API_READ_KEY>"."""
    scheme, _, value = authorization_header.strip().partition(" ")
    expected = read_api_key()
    return bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), expected.encode())


def mcp_key() -> str:
    """Key for the agent's MCP endpoint (`MCP_API_KEY`), separate from the read key because it can write."""
    key = os.environ.get("MCP_API_KEY", "")
    return key if len(key) >= MIN_API_KEY_LENGTH else ""


def mcp_authorized(authorization_header: str) -> bool:
    """True for "Authorization: Bearer <MCP_API_KEY>"."""
    scheme, _, value = authorization_header.strip().partition(" ")
    expected = mcp_key()
    return bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), expected.encode())
