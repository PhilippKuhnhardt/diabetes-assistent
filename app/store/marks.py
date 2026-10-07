"""The user's own analysis decisions: exclusions, day types, vacations, sport marks, manual windows."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.store.db import connect


def analysis_exclusions() -> set[str]:
    with connect() as db:
        rows = db.execute("SELECT meal_id FROM analysis_exclusions").fetchall()
    return {row["meal_id"] for row in rows}


def set_analysis_exclusion(meal_id: str, excluded: bool) -> None:
    with connect() as db:
        if excluded:
            db.execute(
                "INSERT INTO analysis_exclusions(meal_id,excluded_at) VALUES(?,?) "
                "ON CONFLICT(meal_id) DO NOTHING",
                (meal_id, datetime.now(timezone.utc).isoformat()),
            )
        else:
            db.execute("DELETE FROM analysis_exclusions WHERE meal_id=?", (meal_id,))


def analysis_day_types() -> dict[str, str]:
    with connect() as db:
        rows = db.execute("SELECT day, day_type FROM analysis_day_types").fetchall()
    return {row["day"]: row["day_type"] for row in rows}


def set_analysis_day_type(day: str, day_type: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO analysis_day_types(day,day_type) VALUES(?,?) "
            "ON CONFLICT(day) DO UPDATE SET day_type=excluded.day_type",
            (day, day_type),
        )


def vacation_periods() -> list[dict]:
    """Local vacation periods; end_day is None while a vacation is switched on."""
    with connect() as db:
        rows = db.execute("SELECT start_day,end_day FROM vacation_periods ORDER BY start_day").fetchall()
    return [{"start_day": row["start_day"], "end_day": row["end_day"]} for row in rows]


def set_vacation(active: bool, today: str) -> None:
    """Switching on opens a period from today; switching off closes it before today."""
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    with connect() as db:
        open_row = db.execute("SELECT id,start_day FROM vacation_periods WHERE end_day IS NULL").fetchone()
        if active and not open_row:
            db.execute("INSERT INTO vacation_periods(start_day,end_day) VALUES(?,NULL)", (today,))
        elif not active and open_row:
            if open_row["start_day"] >= today:
                db.execute("DELETE FROM vacation_periods WHERE id=?", (open_row["id"],))
            else:
                db.execute("UPDATE vacation_periods SET end_day=? WHERE id=?", (yesterday, open_row["id"]))


def day_type_for(day: str, overrides: dict[str, str], periods: list[dict]) -> tuple[str, str]:
    """Return (day_type, source): a manual choice wins, then vacation, then Mon–Fri work."""
    if day in overrides:
        return overrides[day], "manual"
    if any(period["start_day"] <= day and (period["end_day"] is None or day <= period["end_day"])
           for period in periods):
        return "free", "vacation"
    return ("free" if date.fromisoformat(day).weekday() >= 5 else "work"), "weekday"


def analysis_sport_marks() -> dict[str, dict]:
    with connect() as db:
        rows = db.execute("SELECT meal_id,status,effect_mgdl FROM analysis_sport_marks").fetchall()
    return {row["meal_id"]: {"status": row["status"], "effect_mgdl": row["effect_mgdl"]}
            for row in rows}


def set_analysis_sport_mark(meal_id: str, status: str, effect_mgdl: str = "0") -> None:
    with connect() as db:
        if status == "auto":
            db.execute("DELETE FROM analysis_sport_marks WHERE meal_id=?", (meal_id,))
        else:
            db.execute("INSERT INTO analysis_sport_marks(meal_id,status,effect_mgdl) VALUES(?,?,?) "
                       "ON CONFLICT(meal_id) DO UPDATE SET status=excluded.status,effect_mgdl=excluded.effect_mgdl",
                       (meal_id, status, effect_mgdl))


def analysis_windows() -> dict[str, dict]:
    """Manually chosen meal windows; local analysis cleanup, never sent to Nightscout."""
    with connect() as db:
        rows = db.execute("SELECT meal_id,start_at,end_at FROM analysis_windows").fetchall()
    return {row["meal_id"]: {"start": datetime.fromisoformat(row["start_at"]),
                             "end": datetime.fromisoformat(row["end_at"])}
            for row in rows}


def set_analysis_window(meal_id: str, start: datetime | None, end: datetime | None) -> None:
    with connect() as db:
        if start is None or end is None:
            db.execute("DELETE FROM analysis_windows WHERE meal_id=?", (meal_id,))
        else:
            db.execute(
                "INSERT INTO analysis_windows(meal_id,start_at,end_at,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(meal_id) DO UPDATE SET start_at=excluded.start_at,end_at=excluded.end_at,"
                "updated_at=excluded.updated_at",
                (meal_id, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
                 datetime.now(timezone.utc).isoformat()),
            )
