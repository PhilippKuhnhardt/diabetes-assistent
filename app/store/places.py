"""The user's eating places with their spots, and where each diary carb entry was eaten.
Local only, never sent to Nightscout."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app import places as place_rules
from app.store.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def all_places() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id,name,quick FROM places ORDER BY name COLLATE NOCASE,id").fetchall()
    return [{**dict(row), "quick": bool(row["quick"])} for row in rows]


def all_spots() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id,place_id,lat,lon FROM place_spots ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def place_counts() -> dict[int, int]:
    with connect() as db:
        rows = db.execute("SELECT place_id,COUNT(*) AS n FROM entry_locations WHERE place_id IS NOT NULL "
                          "GROUP BY place_id").fetchall()
    return {row["place_id"]: row["n"] for row in rows}


def _check_name(db: sqlite3.Connection, name: str, place_id: int | None) -> None:
    if db.execute("SELECT 1 FROM places WHERE name=? COLLATE NOCASE AND id IS NOT ?", (name, place_id)).fetchone():
        raise ValueError("Einen Ort mit diesem Namen gibt es schon")


def _check_place(db: sqlite3.Connection, place_id: int) -> None:
    if not db.execute("SELECT 1 FROM places WHERE id=?", (place_id,)).fetchone():
        raise ValueError("Ort nicht gefunden")


def _join_unassigned(db: sqlite3.Connection, spot: dict) -> None:
    """Positions without a place near a spot join its place."""
    rows = db.execute("SELECT event_id,lat,lon FROM entry_locations WHERE place_id IS NULL AND lat IS NOT NULL "
                      "AND accuracy_m <= ?", (place_rules.MAX_ACCURACY_M,))
    joined = [row["event_id"] for row in rows.fetchall()
              if place_rules.match({"lat": row["lat"], "lon": row["lon"]}, [spot])]
    db.executemany("UPDATE entry_locations SET place_id=?,updated_at=? WHERE event_id=?",
                   [(spot["place_id"], _now(), event_id) for event_id in joined])


def create_place(name: str) -> int:
    with connect() as db:
        _check_name(db, name, None)
        return db.execute("INSERT INTO places(name,created_at) VALUES(?,?)", (name, _now())).lastrowid


def rename_place(place_id: int, name: str) -> None:
    with connect() as db:
        _check_place(db, place_id)
        _check_name(db, name, place_id)
        db.execute("UPDATE places SET name=? WHERE id=?", (name, place_id))


def set_quick(place_id: int, quick: bool) -> None:
    """Whether the place has its own chip in the entry form; the others sit behind "Weitere"."""
    with connect() as db:
        _check_place(db, place_id)
        db.execute("UPDATE places SET quick=? WHERE id=?", (int(quick), place_id))


def delete_place(place_id: int) -> None:
    """Entries of a deleted place keep their position and become unknown again."""
    with connect() as db:
        db.execute("UPDATE entry_locations SET place_id=NULL,updated_at=? WHERE place_id=?", (_now(), place_id))
        db.execute("DELETE FROM place_spots WHERE place_id=?", (place_id,))
        db.execute("DELETE FROM places WHERE id=?", (place_id,))
        # Entries recorded without a position carry nothing once their place is gone.
        db.execute("DELETE FROM entry_locations WHERE place_id IS NULL AND lat IS NULL")


def add_spot(place_id: int, spot: dict) -> int:
    with connect() as db:
        _check_place(db, place_id)
        spot_id = db.execute("INSERT INTO place_spots(place_id,lat,lon,created_at) VALUES(?,?,?,?)",
                             (place_id, spot["lat"], spot["lon"], _now())).lastrowid
        _join_unassigned(db, {**spot, "place_id": place_id})
    return spot_id


def delete_spot(spot_id: int) -> None:
    """Entries keep their place; a spot only decides future suggestions and unknown positions."""
    with connect() as db:
        db.execute("DELETE FROM place_spots WHERE id=?", (spot_id,))


def entry_places() -> dict[str, int]:
    """Place id per diary entry (event id) that has one."""
    with connect() as db:
        rows = db.execute("SELECT event_id,place_id FROM entry_locations WHERE place_id IS NOT NULL").fetchall()
    return {row["event_id"]: row["place_id"] for row in rows}


def unassigned_locations() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT event_id,at,lat,lon FROM entry_locations WHERE place_id IS NULL "
                          "AND lat IS NOT NULL AND accuracy_m <= ? ORDER BY at",
                          (place_rules.MAX_ACCURACY_M,)).fetchall()
    return [dict(row) for row in rows]


def set_entry_location(event_id: str, at: datetime, place_id: int | None, position: dict | None) -> None:
    """Record where an entry was eaten. A position, once stored, is kept when only the place changes."""
    with connect() as db:
        if place_id is not None:
            _check_place(db, place_id)
        existing = db.execute("SELECT lat FROM entry_locations WHERE event_id=?", (event_id,)).fetchone()
        if position is None and place_id is None and (existing is None or existing["lat"] is None):
            db.execute("DELETE FROM entry_locations WHERE event_id=?", (event_id,))
            return
        stamp = at.astimezone(timezone.utc).isoformat()
        if position is None:
            db.execute("INSERT INTO entry_locations(event_id,at,place_id,updated_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(event_id) DO UPDATE SET at=excluded.at,place_id=excluded.place_id,"
                       "updated_at=excluded.updated_at", (event_id, stamp, place_id, _now()))
        else:
            db.execute("INSERT INTO entry_locations(event_id,at,lat,lon,accuracy_m,place_id,updated_at) "
                       "VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET at=excluded.at,lat=excluded.lat,"
                       "lon=excluded.lon,accuracy_m=excluded.accuracy_m,place_id=excluded.place_id,"
                       "updated_at=excluded.updated_at",
                       (event_id, stamp, position["lat"], position["lon"], position["accuracy_m"], place_id, _now()))
