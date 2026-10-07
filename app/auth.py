"""Small persistent, password-based browser session."""
from __future__ import annotations

import hashlib
import hmac
import os
from http.cookies import SimpleCookie

COOKIE_NAME = "diary_session"
# A later write key would get its own variable, never reuse the read key.
MIN_API_KEY_LENGTH = 32
MAX_AGE = 365 * 24 * 3600


def password() -> str:
    return os.environ.get("APP_PASSWORD", "")


def token() -> str:
    return hmac.new(password().encode(), b"diabetes-diary-session-v1", hashlib.sha256).hexdigest()


def authorized(cookie_header: str) -> bool:
    if not password():
        return True
    try:
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        supplied = cookies[COOKIE_NAME].value
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(supplied, token())


def cookie_header(secure: bool = False, clear: bool = False) -> str:
    value = "" if clear else token()
    header = f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={0 if clear else MAX_AGE}"
    return header + ("; Secure" if secure else "")


def read_api_key() -> str:
    """Read-only key for scripts and agents (`API_READ_KEY`); too short or unset disables key access."""
    key = os.environ.get("API_READ_KEY", "")
    return key if len(key) >= MIN_API_KEY_LENGTH else ""


def api_read_authorized(authorization_header: str) -> bool:
    """True for "Authorization: Bearer <API_READ_KEY>"."""
    scheme, _, value = authorization_header.strip().partition(" ")
    expected = read_api_key()
    return bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), expected.encode())
