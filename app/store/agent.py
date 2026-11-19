"""The meal agent's settings, estimate sessions, usage log and photo files. Local only."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.store import db as database
from app.store.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def settings() -> dict[str, str]:
    with connect() as db:
        return {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM agent_settings").fetchall()}


def set_settings(values: dict[str, str]) -> None:
    with connect() as db:
        for key, value in values.items():
            db.execute("INSERT INTO agent_settings(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _session(row) -> dict | None:
    if row is None:
        return None
    return {**dict(row), "messages": json.loads(row["messages"]),
            "estimate": json.loads(row["estimate"]) if row["estimate"] else None}


def session(session_id: str) -> dict | None:
    with connect() as db:
        return _session(db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,)).fetchone())


def sessions_for(event_ids: list[str]) -> dict[str, dict]:
    """The newest linked session per diary entry (event id)."""
    if not event_ids:
        return {}
    with connect() as db:
        rows = db.execute("SELECT * FROM agent_sessions WHERE event_id IN (%s) ORDER BY updated_at"
                          % ",".join("?" * len(event_ids)), event_ids).fetchall()
    return {row["event_id"]: _session(row) for row in rows}


def save_session(session_id: str, at: datetime, provider: str, model: str, messages: list[dict],
                 estimate: dict | None, photos: int, usage: dict) -> None:
    now = _now()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_sessions(id,created_at,updated_at,at,provider,model,messages,estimate,photos,calls,"
            "input_tokens,output_tokens,cost_usd) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,provider=excluded.provider,"
            "model=excluded.model,messages=excluded.messages,estimate=excluded.estimate,photos=excluded.photos,"
            "calls=calls+1,input_tokens=input_tokens+excluded.input_tokens,"
            "output_tokens=output_tokens+excluded.output_tokens,"
            "cost_usd=CASE WHEN excluded.cost_usd IS NULL THEN cost_usd ELSE COALESCE(cost_usd,0)+excluded.cost_usd END",
            (session_id, now, now, at.astimezone(timezone.utc).isoformat(), provider, model,
             json.dumps(messages, ensure_ascii=False), json.dumps(estimate, ensure_ascii=False) if estimate else None,
             photos, usage.get("input_tokens") or 0, usage.get("output_tokens") or 0, usage.get("cost_usd")))


def link_session(session_id: str, event_id: str) -> None:
    """The estimate belongs to this diary entry from now on."""
    with connect() as db:
        db.execute("UPDATE agent_sessions SET event_id=?,updated_at=? WHERE id=?", (event_id, _now(), session_id))


def log_usage(kind: str, provider: str, model: str, usage: dict) -> None:
    with connect() as db:
        db.execute("INSERT INTO agent_usage(at,kind,provider,model,input_tokens,output_tokens,cost_usd) "
                   "VALUES(?,?,?,?,?,?,?)", (_now(), kind, provider, model, usage.get("input_tokens") or 0,
                                             usage.get("output_tokens") or 0, usage.get("cost_usd")))


def usage_since(start: datetime) -> list[dict]:
    """Calls, tokens and (where the provider names it) cost per provider and model since `start`."""
    with connect() as db:
        rows = db.execute(
            "SELECT provider,model,kind,COUNT(*) AS calls,SUM(input_tokens) AS input_tokens,"
            "SUM(output_tokens) AS output_tokens,SUM(cost_usd) AS cost_usd FROM agent_usage WHERE at>=? "
            "GROUP BY provider,model,kind ORDER BY calls DESC", (start.astimezone(timezone.utc).isoformat(),)).fetchall()
    return [dict(row) for row in rows]


def photo_dir() -> Path:
    return database.DB_PATH.parent / "photos"


def photo_path(session_id: str, index: int) -> Path:
    return photo_dir() / session_id / f"{index}.jpg"


def save_photos(session_id: str, photos: list[bytes], start: int = 0) -> None:
    """Photos of an estimate, numbered from `start` (photos added in a follow-up come after the first ones)."""
    if not photos:
        return
    folder = photo_dir() / session_id
    folder.mkdir(parents=True, exist_ok=True)
    for index, data in enumerate(photos, start):
        (folder / f"{index}.jpg").write_bytes(data)


def load_photos(session_id: str, count: int) -> list[bytes]:
    return [photo_path(session_id, index).read_bytes() for index in range(count)
            if photo_path(session_id, index).is_file()]


def delete_session(session_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
    shutil.rmtree(photo_dir() / session_id, ignore_errors=True)
