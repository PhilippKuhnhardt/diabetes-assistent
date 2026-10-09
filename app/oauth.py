"""OAuth 2.1 sign-in for the MCP endpoint, so the Claude apps can connect as a custom connector.

The Claude apps only connect to remote MCP servers through OAuth: they register themselves, send
Philipp to /oauth/authorize, where the app password confirms the connection, and then use short-lived
access tokens. Tokens are signed with a key derived from MCP_API_KEY and stored nowhere, so changing
MCP_API_KEY revokes every connection. Without MCP_API_KEY all of this is off.
Redirects go only to Claude's callback and to loopback addresses (Claude Code), never to an address a
registration names, so a forged connect link cannot send a code to someone else.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode, urlparse

from app import auth

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 90 * 24 * 3600
CODE_SECONDS = 600
CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

_codes: dict[str, dict] = {}
_codes_lock = threading.Lock()


def enabled() -> bool:
    return bool(auth.mcp_key())


def _key() -> bytes:
    return hmac.new(auth.mcp_key().encode(), b"diabetes-mcp-oauth-v1", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(kind: str, seconds: int) -> str:
    body = _b64(json.dumps({"typ": kind, "exp": int(time.time()) + seconds, "n": secrets.token_hex(8)}).encode())
    return body + "." + _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def _valid(token: str, kind: str) -> bool:
    body, _, signature = token.partition(".")
    if not enabled() or not body or not signature:
        return False
    try:
        if not hmac.compare_digest(_unb64(signature), hmac.new(_key(), body.encode(), hashlib.sha256).digest()):
            return False
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return False
    return payload.get("typ") == kind and int(payload.get("exp", 0)) > time.time()


def access_authorized(authorization_header: str) -> bool:
    scheme, _, value = authorization_header.strip().partition(" ")
    return scheme.lower() == "bearer" and _valid(value.strip(), "access")


def base_url(headers) -> str:
    proto = headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"
    return f"{proto}://{headers.get('Host', 'localhost')}"


def protected_resource(base: str) -> dict:
    return {"resource": base + "/mcp", "authorization_servers": [base], "bearer_methods_supported": ["header"],
            "resource_name": "Diabetes-Tagebuch"}


def authorization_server(base: str) -> dict:
    return {
        "issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register", "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
    }


def allowed_redirect(uri: str) -> bool:
    extra = [item.strip() for item in os.environ.get("MCP_OAUTH_REDIRECTS", "").split(",") if item.strip()]
    if uri in CLAUDE_CALLBACKS or uri in extra:
        return True
    parsed = urlparse(uri)
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS and not parsed.username


def register(payload: dict) -> dict:
    """Dynamic client registration: any client may register, but only for the allowed redirects."""
    uris = payload.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(uri, str) and allowed_redirect(uri) for uri in uris):
        raise ValueError("redirect_uris nicht erlaubt")
    return {"client_id": "mcp-" + secrets.token_hex(12), "client_id_issued_at": int(time.time()),
            "redirect_uris": uris, "client_name": str(payload.get("client_name") or "")[:100],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}


def authorize_request(params: dict) -> dict:
    """The checked fields of an authorization request (query string or the confirmation form)."""
    value = {key: str(params.get(key, [""])[0]) for key in
             ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state")}
    if not enabled():
        raise ValueError("MCP ist nicht eingerichtet")
    if value["response_type"] != "code" or not value["client_id"]:
        raise ValueError("Ungültige Anfrage")
    if not allowed_redirect(value["redirect_uri"]):
        raise ValueError("Diese Rücksprungadresse ist nicht erlaubt")
    if not value["code_challenge"] or value["code_challenge_method"] != "S256":
        raise ValueError("PKCE (S256) fehlt")
    return value


def authorize_page(request: dict, error: bool = False) -> str:
    fields = "".join(f'<input type="hidden" name="{key}" value="{html.escape(value)}">'
                     for key, value in request.items())
    target = html.escape(urlparse(request["redirect_uri"]).netloc)
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent verbinden · Tagebuch</title>
  <link rel="icon" type="image/svg+xml" href="/logo.svg">
  <link rel="stylesheet" href="/styles.css">
</head>
<body class="login-page">
  <main class="login-card panel">
    <div class="login-brand"><img src="/logo.svg" width="48" height="48" alt=""><span>Diabetes-Tagebuch</span></div>
    <h1>Agent verbinden</h1>
    <p>Ein Agent über <strong>{target}</strong> möchte deine Werte lesen und Einträge speichern.
       Mit dem Passwort erlaubst du das.</p>
    <form action="/oauth/authorize" method="post">
      {fields}
      <label>Passwort
        <input name="password" type="password" autocomplete="current-password" required autofocus>
      </label>
      {'<p class="form-error">Passwort stimmt nicht.</p>' if error else ''}
      <button class="primary" type="submit">Verbinden</button>
    </form>
  </main>
</body>
</html>"""


def authorize_confirm(request: dict, password: str) -> str | None:
    """The redirect with a one-time code, or None when the password is wrong."""
    expected = auth.password()
    if not expected or not hmac.compare_digest(password.encode(), expected.encode()):
        return None
    code = secrets.token_urlsafe(32)
    with _codes_lock:
        now = time.time()
        for stale in [key for key, item in _codes.items() if item["exp"] < now]:
            del _codes[stale]
        _codes[code] = {"client_id": request["client_id"], "redirect_uri": request["redirect_uri"],
                        "challenge": request["code_challenge"], "exp": now + CODE_SECONDS}
    query = {"code": code, **({"state": request["state"]} if request["state"] else {})}
    return request["redirect_uri"] + ("&" if "?" in request["redirect_uri"] else "?") + urlencode(query)


def _tokens() -> dict:
    return {"access_token": _sign("access", ACCESS_SECONDS), "token_type": "Bearer", "expires_in": ACCESS_SECONDS,
            "refresh_token": _sign("refresh", REFRESH_SECONDS)}


def token(form: dict) -> dict:
    """Token endpoint; raises ValueError with the OAuth error code."""
    value = {key: str(form.get(key, [""])[0]) for key in
             ("grant_type", "code", "redirect_uri", "client_id", "code_verifier", "refresh_token")}
    if not enabled():
        raise ValueError("invalid_client")
    if value["grant_type"] == "refresh_token":
        if not _valid(value["refresh_token"], "refresh"):
            raise ValueError("invalid_grant")
        return _tokens()
    if value["grant_type"] != "authorization_code":
        raise ValueError("unsupported_grant_type")
    with _codes_lock:
        issued = _codes.pop(value["code"], None)
    challenge = _b64(hashlib.sha256(value["code_verifier"].encode()).digest())
    if (issued is None or issued["exp"] < time.time() or issued["client_id"] != value["client_id"]
            or issued["redirect_uri"] != value["redirect_uri"]
            or not hmac.compare_digest(challenge, issued["challenge"])):
        raise ValueError("invalid_grant")
    return _tokens()
