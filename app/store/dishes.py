"""The dish catalogue and the meal record of each diary carb entry (its dish and description).
Local only, never sent to Nightscout."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.store.db import connect
from app.i18n import tr


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def all_dishes() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id,name,category,archived FROM dishes ORDER BY name COLLATE NOCASE,id").fetchall()
    return [{**dict(row), "archived": bool(row["archived"])} for row in rows]


def _check_name(db: sqlite3.Connection, name: str, dish_id: int | None) -> None:
    if db.execute("SELECT 1 FROM dishes WHERE name=? COLLATE NOCASE AND id IS NOT ?", (name, dish_id)).fetchone():
        raise ValueError(tr("Ein Gericht mit diesem Namen gibt es schon"))


def _check_dish(db: sqlite3.Connection, dish_id: int) -> None:
    if not db.execute("SELECT 1 FROM dishes WHERE id=?", (dish_id,)).fetchone():
        raise ValueError(tr("Gericht nicht gefunden"))


def create_dish(name: str, category: str = "") -> int:
    with connect() as db:
        _check_name(db, name, None)
        return db.execute("INSERT INTO dishes(name,category,created_at) VALUES(?,?,?)",
                          (name, category, _now())).lastrowid


def dish_named(name: str) -> int | None:
    with connect() as db:
        row = db.execute("SELECT id FROM dishes WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return row["id"] if row else None


def update_dish(dish_id: int, name: str | None = None, category: str | None = None,
                archived: bool | None = None) -> None:
    with connect() as db:
        _check_dish(db, dish_id)
        if name is not None:
            _check_name(db, name, dish_id)
            db.execute("UPDATE dishes SET name=? WHERE id=?", (name, dish_id))
        if category is not None:
            db.execute("UPDATE dishes SET category=? WHERE id=?", (category, dish_id))
        if archived is not None:
            db.execute("UPDATE dishes SET archived=? WHERE id=?", (int(archived), dish_id))


def merge_dish(dish_id: int, into_id: int) -> None:
    """Every record of the dish moves to the other one; the dish itself goes."""
    with connect() as db:
        _check_dish(db, dish_id)
        _check_dish(db, into_id)
        if dish_id == into_id:
            raise ValueError(tr("Ein Gericht kann nicht mit sich selbst zusammengelegt werden"))
        db.execute("UPDATE meal_records SET dish_id=?,updated_at=? WHERE dish_id=?", (into_id, _now(), dish_id))
        db.execute("DELETE FROM dishes WHERE id=?", (dish_id,))


def delete_dish(dish_id: int) -> None:
    """Entries of a deleted dish keep their description and lose the dish."""
    with connect() as db:
        db.execute("UPDATE meal_records SET dish_id=NULL,updated_at=? WHERE dish_id=?", (_now(), dish_id))
        db.execute("DELETE FROM meal_records WHERE dish_id IS NULL AND description=''")
        db.execute("DELETE FROM dishes WHERE id=?", (dish_id,))


def records() -> dict[str, dict]:
    """Meal record per diary entry (event id): dish id and description."""
    with connect() as db:
        rows = db.execute("SELECT event_id,dish_id,description FROM meal_records").fetchall()
    return {row["event_id"]: dict(row) for row in rows}


def dish_records(since: datetime | None = None) -> list[dict]:
    """Every record that names a dish, oldest first: when it was eaten and how many carbs were entered."""
    with connect() as db:
        rows = db.execute("SELECT event_id,at,dish_id,carbs_g FROM meal_records WHERE dish_id IS NOT NULL "
                          "AND at>=? ORDER BY at", ((since or datetime.min.replace(tzinfo=timezone.utc))
                                                    .astimezone(timezone.utc).isoformat(),)).fetchall()
    return [dict(row) for row in rows]


def set_record(event_id: str, at: datetime, dish_id: int | None, description: str, carbs_g: float | None) -> None:
    """Record what an entry was; neither dish nor description removes the record."""
    with connect() as db:
        if dish_id is None and not description:
            db.execute("DELETE FROM meal_records WHERE event_id=?", (event_id,))
            return
        if dish_id is not None:
            _check_dish(db, dish_id)
        db.execute("INSERT INTO meal_records(event_id,at,dish_id,description,carbs_g,updated_at) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(event_id) DO UPDATE SET at=excluded.at,dish_id=excluded.dish_id,"
                   "description=excluded.description,carbs_g=excluded.carbs_g,updated_at=excluded.updated_at",
                   (event_id, at.astimezone(timezone.utc).isoformat(), dish_id, description, carbs_g, _now()))


def move_record(event_id: str, at: datetime, carbs_g: float | None) -> None:
    """An edited entry keeps its record at the new time and carbs."""
    with connect() as db:
        db.execute("UPDATE meal_records SET at=?,carbs_g=?,updated_at=? WHERE event_id=?",
                   (at.astimezone(timezone.utc).isoformat(), carbs_g, _now(), event_id))


def delete_record(event_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM meal_records WHERE event_id=?", (event_id,))
