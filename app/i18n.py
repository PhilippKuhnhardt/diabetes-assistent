"""Language of the server's own texts (2026-11-26): error messages, hints in the day and analysis
data, push notifications, and the meal agent's prompts and tool descriptions.

APP_LANGUAGE in the deployment picks it: "de" (default) or "en". The page itself follows the browser
or its own setting (web/ui/i18n.js); this one is fixed for the process, so module-level labels can be
built at import.

German is the source: a text is written in German and wrapped as tr("Text mit {platzhalter}", platzhalter=…).
In English, tr() looks the German text up in app/i18n_en.py; a text missing there stays German
(tests/test_i18n.py checks that none is missing). Long prompts pick one of two versions with pick()."""
from __future__ import annotations

import os

LANGUAGES = ("de", "en")


def _configured() -> str:
    value = os.environ.get("APP_LANGUAGE", "de").strip().lower()[:2]
    return value if value in LANGUAGES else "de"


LANGUAGE = _configured()


def english() -> bool:
    return LANGUAGE == "en"


def tr(text: str, **params) -> str:
    """The text in the configured language, placeholders filled in."""
    if LANGUAGE == "en":
        from app.i18n_en import EN
        text = EN.get(text, text)
    return text.format(**params) if params else text


def pick(de: str, en: str) -> str:
    """One of two whole versions (prompts too long to key by their German text)."""
    return en if LANGUAGE == "en" else de
