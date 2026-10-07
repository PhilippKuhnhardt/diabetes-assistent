"""Small persistent, password-based browser session."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from http.cookies import SimpleCookie

COOKIE_NAME = "diary_session"
API_KEY_PREFIX = "da_"
# Scopes an API key can carry. Only reading exists; writing would get its own scope later.
API_SCOPES = ("read",)
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


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def api_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def bearer_key(authorization_header: str) -> str | None:
    """The key of an "Authorization: Bearer da_…" header, None when absent or malformed."""
    scheme, _, value = authorization_header.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value.startswith(API_KEY_PREFIX) or len(value) > 200:
        return None
    return value
