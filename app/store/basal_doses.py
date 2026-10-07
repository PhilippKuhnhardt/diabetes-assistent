"""Basal injections, stored locally first and mirrored to Nightscout."""
from __future__ import annotations

from datetime import datetime, timezone

from app.store.db import connect


def basal_doses_between(start: datetime, end: datetime) -> list[dict]:
    """Locally stored basal injections; the app's own record, mirrored to Nightscout when possible."""
    with connect() as db:
        rows = db.execute("SELECT id,at,units,notes,nightscout_synced FROM basal_doses WHERE at>=? AND at<? ORDER BY at",
                          (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()
    return [{"id": row["id"], "at": datetime.fromisoformat(row["at"]), "units": row["units"],
             "notes": row["notes"], "nightscout_synced": bool(row["nightscout_synced"])} for row in rows]


def save_basal_dose(dose_id: str, at: datetime, units: str, notes: str = "", synced: bool = False) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO basal_doses(id,at,units,notes,created_at,nightscout_synced) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET at=excluded.at,units=excluded.units,notes=excluded.notes",
            (dose_id, at.astimezone(timezone.utc).isoformat(), units, notes,
             datetime.now(timezone.utc).isoformat(), int(synced)),
        )


def set_basal_synced(dose_id: str, synced: bool = True) -> None:
    with connect() as db:
        db.execute("UPDATE basal_doses SET nightscout_synced=? WHERE id=?", (int(synced), dose_id))


def delete_basal_dose(dose_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM basal_doses WHERE id=?", (dose_id,))


def basal_dose(dose_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT id,at,units,notes,nightscout_synced FROM basal_doses WHERE id=?", (dose_id,)).fetchone()
    return {"id": row["id"], "at": datetime.fromisoformat(row["at"]), "units": row["units"], "notes": row["notes"],
            "nightscout_synced": bool(row["nightscout_synced"])} if row else None
