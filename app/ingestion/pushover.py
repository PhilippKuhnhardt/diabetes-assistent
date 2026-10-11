"""Pushover: push messages to Philipp's phone (user decision 2026-10-09, research in project files
analysen/benachrichtigungen.md). Token and user key come from the deployment environment."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.pushover.net/1/messages.json"
# High priority: skips Pushover's quiet hours; the iOS app can play it as a critical alert if enabled there.
PRIORITY = 1
# Emergency (2) repeats until acknowledged: every RETRY seconds, for at most EXPIRE seconds.
EMERGENCY = 2
EMERGENCY_RETRY, EMERGENCY_EXPIRE = 60, 1800


class PushoverError(Exception):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("PUSHOVER_APP_TOKEN") and os.environ.get("PUSHOVER_USER_KEY"))


def send(title: str, message: str, priority: int = PRIORITY) -> None:
    """One message to the configured user; raises PushoverError when Pushover does not accept it."""
    if not is_configured():
        raise PushoverError("Pushover ist nicht eingerichtet")
    fields = {"token": os.environ["PUSHOVER_APP_TOKEN"], "user": os.environ["PUSHOVER_USER_KEY"],
              "title": title[:250], "message": message[:1024], "priority": str(priority)}
    if priority == EMERGENCY:
        fields.update({"retry": str(EMERGENCY_RETRY), "expire": str(EMERGENCY_EXPIRE)})
    public_url = os.environ.get("APP_PUBLIC_URL", "").strip()
    if public_url.startswith("https://"):
        fields.update({"url": public_url, "url_title": "App öffnen"})
    request = urllib.request.Request(API_URL, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            answer = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        # Pushover explains rejected requests in "errors"; never echo the token or user key.
        try:
            errors = json.loads(error.read() or b"{}").get("errors") or []
        except ValueError:
            errors = []
        raise PushoverError(f"Pushover lehnt ab ({error.code}): " + "; ".join(map(str, errors))[:300]) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise PushoverError(f"Pushover nicht erreichbar: {type(error).__name__}") from None
    if answer.get("status") != 1:
        raise PushoverError("Pushover lehnt ab: " + "; ".join(map(str, answer.get("errors") or []))[:300])


def heartbeat() -> None:
    """Optional external watchdog (e.g. Healthchecks.io): pinged after every successful background round,
    so a stopped server or job alarms from outside. Failures are ignored; the watchdog notices them."""
    url = os.environ.get("APP_HEARTBEAT_URL", "").strip()
    if not url.startswith("https://"):
        return
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10):
            pass
    except (urllib.error.URLError, TimeoutError, ValueError):
        pass
