"""Read-only views of the local mirror: read API and ZIP export."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from app import export
from app.common import parse_instant
from app.services import day_view
from app.store import config, marks, mirror


class ApiNotFound(Exception):
    pass


API_TABLES = {"meals": "meals", "window-events": "window_events", "entries": "entries",
              "treatments": "treatments", "basal": "basal"}
API_MAX_DAYS = 3650


def api_day_range(params: dict, today: date) -> tuple[date, date]:
    """Local days for the read API: from/to (YYYY-MM-DD, inclusive) or the last `days` days (default 30)."""
    if "from" in params or "to" in params:
        try:
            last = date.fromisoformat(params["to"][0]) if "to" in params else today
            first = date.fromisoformat(params["from"][0]) if "from" in params else last - timedelta(days=29)
        except ValueError:
            raise ValueError("Ungültiges Datum, erwartet JJJJ-MM-TT") from None
    else:
        days = int(params.get("days", ["30"])[0])
        if not 1 <= days <= API_MAX_DAYS:
            raise ValueError("Ungültiger Zeitraum")
        first, last = today - timedelta(days=days - 1), today
    last = min(last, today)
    if first > last or (last - first).days >= API_MAX_DAYS:
        raise ValueError("Ungültiger Zeitraum")
    return first, last


def api_read(path: str, query: str) -> dict:
    """Read-only JSON view of the local SQLite mirror (same rows as the ZIP export); never asks Nightscout."""
    _, settings = config.get_settings()
    zone = ZoneInfo(settings["timezone"])
    today = datetime.now(zone).date()
    first_at = mirror.first_cached_at()
    first_cached = parse_instant(first_at).astimezone(zone).date() if first_at else None
    if path in ("/api/v1", "/api/v1/"):
        return {
            "version": 1, "timezone": settings["timezone"], "today": today.isoformat(),
            "first_day": first_cached.isoformat() if first_cached else None,
            "query": "?days=N (Standard 30) oder ?from=JJJJ-MM-TT&to=JJJJ-MM-TT, lokale Tage inklusive",
            "endpoints": {
                "/api/v1/data": "alle Tabellen auf einmal",
                **{f"/api/v1/{name}": f"Spalten: {', '.join(export.TABLE_COLUMNS[table])}"
                   for name, table in API_TABLES.items()},
                "/api/v1/profile": "Einstellungen und aktuelles Nightscout-Profil",
            },
            "notes": export.README,
        }
    if path == "/api/v1/profile":
        return {"timezone": settings["timezone"], "settings": settings}
    name = path.removeprefix("/api/v1/")
    if name != "data" and name not in API_TABLES:
        raise ApiNotFound(path)
    first, last = api_day_range(parse_qs(query), today)
    loaded_first = max(first, first_cached) if first_cached else last + timedelta(days=1)
    tables = export.collect(loaded_first, last, lambda day: day_view.day_payload(day, refresh=False),
                            marks.analysis_exclusions(), settings)
    tables = {table: export.rows_for(table, rows) for table, rows in tables.items()}
    result = {"from": first.isoformat(), "to": last.isoformat(), "timezone": settings["timezone"]}
    if name == "data":
        return {**result, "tables": tables}
    return {**result, "rows": tables[API_TABLES[name]]}


def export_zip(params: dict) -> tuple[str, bytes]:
    """ZIP of all locally stored days (or the last `days`); read-only snapshot, no Nightscout requests."""
    _, settings = config.get_settings()
    zone = ZoneInfo(settings["timezone"])
    today = datetime.now(zone).date()
    first_at = mirror.first_cached_at()
    first_day = min(today, parse_instant(first_at).astimezone(zone).date()) if first_at else today
    if "days" in params:
        days = int(params["days"][0])
        if not 1 <= days <= 3650:
            raise ValueError("Ungültiger Zeitraum")
        first_day = max(first_day, today - timedelta(days=days - 1))
    data = export.build_zip(first_day, today, lambda day: day_view.day_payload(day, refresh=False),
                            marks.analysis_exclusions(), settings)
    return f"diabetes-export-{today.isoformat()}.zip", data
