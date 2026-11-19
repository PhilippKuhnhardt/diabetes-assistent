"""The meal agent: estimate a meal from photos and a description, transcribe a voice note, and the
agent's settings (provider, model, web search). The rules are in app/meal_agent.py, the provider
calls in app/ingestion/llm.py; this module joins them with the dish catalogue and stores each estimate."""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timezone

from app import episodes, meal_agent as rules
from app.common import local_zone, parse_instant
from app.ingestion import llm
from app.services import dishes
from app.store import agent as store, config, dishes as dish_store, places as place_store

DEFAULT_TRANSCRIBE_MODEL = "google/gemini-2.5-flash"
TRANSCRIBE_INSTRUCTION = ("Transkribiere diese Sprachnotiz wörtlich auf Deutsch. Es geht um eine Mahlzeit "
                          "(Lebensmittel, Mengen in Gramm, Marken). Gib nur den gesprochenen Text zurück, "
                          "ohne Anführungszeichen und ohne Kommentar. Ist nichts zu verstehen, gib nichts zurück.")
MAX_AUDIO_BYTES = 6_000_000
AUDIO_FORMATS = ("wav", "mp3")


def _settings() -> dict:
    stored = store.settings()
    available = llm.configured()
    provider = stored.get("provider") if stored.get("provider") in available else (available[0] if available else None)
    return {
        "provider": provider,
        "models": {name: stored.get("model_" + name) or llm.PROVIDERS[name]["default_model"] for name in llm.PROVIDERS},
        "web_search": stored.get("web_search", "1") == "1",
        "transcribe_model": stored.get("transcribe_model") or DEFAULT_TRANSCRIBE_MODEL,
    }


def view(params: dict | None = None) -> dict:
    """What the page needs to offer the agent and its settings; never a key."""
    current = _settings()
    zone = local_zone(config.get_settings()[1]["timezone"])
    month = datetime.now(zone).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "available": bool(current["provider"] and current["models"][current["provider"]]),
        "provider": current["provider"],
        "providers": [{"id": name, "label": info["label"], "key": info["key"], "configured": bool(llm.api_key(name)),
                       "model": current["models"][name]} for name, info in llm.PROVIDERS.items()],
        "web_search": current["web_search"],
        "transcribe_model": current["transcribe_model"],
        "transcribe_available": bool(llm.api_key("openrouter")),
        "limits": {"photos": rules.MAX_PHOTOS, "session_photos": rules.MAX_SESSION_PHOTOS, "text": rules.MAX_TEXT},
        "usage_month": store.usage_since(month),
    }


def save_settings(payload: dict) -> dict:
    values = {}
    if "provider" in payload:
        if payload["provider"] not in llm.configured():
            raise ValueError("Für diesen Anbieter ist kein Schlüssel hinterlegt")
        values["provider"] = payload["provider"]
    for name in llm.PROVIDERS:
        if "model_" + name in payload:
            values["model_" + name] = str(payload["model_" + name] or "").strip()[:120]
    if "web_search" in payload:
        values["web_search"] = "1" if payload["web_search"] is True else "0"
    if "transcribe_model" in payload:
        values["transcribe_model"] = str(payload["transcribe_model"] or "").strip()[:120]
    store.set_settings(values)
    return view()


def models(params: dict) -> dict:
    """The provider's models for the settings' pick list."""
    provider = (params.get("provider") or [""])[0]
    try:
        return {"provider": provider, "models": llm.models(provider)}
    except llm.LLMError as error:
        raise ValueError(str(error)) from None


def _photos(raw) -> list[bytes]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > rules.MAX_PHOTOS:
        raise ValueError(f"Höchstens {rules.MAX_PHOTOS} Fotos")
    photos = []
    for item in raw:
        text = str(item or "")
        try:
            data = base64.b64decode(text.split(",", 1)[1] if text.startswith("data:") else text, validate=True)
        except (binascii.Error, ValueError, IndexError):
            raise ValueError("Foto konnte nicht gelesen werden") from None
        if not data.startswith(b"\xff\xd8") or len(data) > rules.MAX_PHOTO_BYTES:
            raise ValueError("Foto muss ein JPEG bis 3 MB sein")
        photos.append(data)
    return photos


def _examples() -> dict[int, list[dict]]:
    """Recent entries per dish with what was written about them."""
    zone = local_zone(config.get_settings()[1]["timezone"])
    described = dish_store.records()
    result: dict[int, list[dict]] = {}
    for record in dish_store.dish_records():
        result.setdefault(record["dish_id"], []).append({
            "day": parse_instant(record["at"]).astimezone(zone).date().isoformat(), "carbs_g": record["carbs_g"],
            "description": (described.get(record["event_id"]) or {}).get("description") or ""})
    return result


def _call_tool(name: str, args: dict) -> str:
    """Answer one of the agent's lookups as JSON text; a bad call gets its reason back, never an exception."""
    try:
        if name == "search_meals":
            result = dishes.lookup_meals(args.get("query") or "", args.get("meal") or None, args.get("place") or None,
                                         args.get("dish") or None, args.get("days") or dishes.VIEW_DAYS)
        elif name == "dish_history":
            wanted = " ".join(str(args.get("dish") or "").split()).casefold()
            days = max(1, min(int(args.get("days") or dishes.VIEW_DAYS), dishes.VIEW_MAX_DAYS))
            catalogue = dishes.view({"days": [str(days)]})["dishes"]
            found = next((dish for dish in catalogue if dish["name"].casefold() == wanted), None)
            result = found if found else {"error": "Unbekanntes Gericht. Vorhandene Gerichte: "
                                                   + (", ".join(dish["name"] for dish in catalogue) or "keine")}
        else:
            result = {"error": f"Unbekanntes Werkzeug {name}"}
    except (TypeError, ValueError) as error:
        result = {"error": str(error) or "Ungültige Anfrage"}
    return json.dumps(result, ensure_ascii=False, default=str)


def estimate(payload: dict) -> dict:
    """One turn with the agent: a new meal (text and/or photos) or a follow-up to its last answer."""
    current = _settings()
    provider = current["provider"]
    if not provider:
        raise ValueError("Kein Anbieter eingerichtet: ANTHROPIC_API_KEY, OPENAI_API_KEY oder OPENROUTER_API_KEY "
                         "in der Bereitstellung setzen")
    model = current["models"][provider]
    text = str(payload.get("text") or "").strip()[:rules.MAX_TEXT]
    at = parse_instant(str(payload["at"])) if payload.get("at") else datetime.now(timezone.utc)
    session = store.session(str(payload["session_id"])) if payload.get("session_id") else None
    if payload.get("session_id") and session is None:
        raise ValueError("Einschätzung nicht gefunden")
    new_photos = _photos(payload.get("photos"))
    if session:
        if not text and not new_photos:
            raise ValueError("Bitte eine Antwort, Korrektur oder ein Foto schicken")
        if len(session["messages"]) >= rules.MAX_TURNS * 2:
            raise ValueError("Zu viele Rückfragen, bitte neu einschätzen")
        if session["photos"] + len(new_photos) > rules.MAX_SESSION_PHOTOS:
            raise ValueError(f"Höchstens {rules.MAX_SESSION_PHOTOS} Fotos je Einschätzung")
        session_id, earlier = session["id"], session["photos"]
        photos = store.load_photos(session_id, earlier) + new_photos
        history = session["messages"]
    else:
        if not text and not new_photos:
            raise ValueError("Bitte ein Foto aufnehmen oder das Essen beschreiben")
        session_id, earlier, photos, history = str(uuid.uuid4()), 0, new_photos, []
    _, settings = config.get_settings()
    zone = local_zone(settings["timezone"])
    catalogue = dishes.view()["dishes"]
    place_id = payload.get("place_id")
    place = next((item["name"] for item in place_store.all_places() if str(item["id"]) == str(place_id)), None)
    context = rules.context_text(at.astimezone(zone).strftime("%A, %d.%m.%Y %H:%M"),
                                 episodes.meal_kind_at(at, settings)[1], place,
                                 rules.catalogue_text(catalogue, _examples()),
                                 [item["name"] for item in place_store.all_places()])
    turns = history + [{"role": "user", "text": text or "(nur Fotos)", "photos": len(new_photos)}]
    encoded = [base64.b64encode(photo).decode() for photo in photos]
    shares = _photo_shares(turns, len(photos))
    messages = [{"role": turn["role"],
                 "text": (context + "\n\nPhilipp: " + turn["text"]) if index == 0 else turn["text"],
                 "images": encoded[shares[index][0]:shares[index][1]]} for index, turn in enumerate(turns)]
    try:
        answer = llm.run(provider, model, rules.SYSTEM, messages, rules.SCHEMA, rules.TOOLS, _call_tool,
                         current["web_search"])
        result = rules.check(answer["data"], catalogue)
    except llm.LLMError as error:
        raise ValueError(str(error)) from None
    result["sources"] = answer["sources"]
    result["lookups"] = rules.lookups(answer.get("tool_calls") or [])
    store.save_photos(session_id, new_photos, start=earlier)
    turns.append({"role": "assistant", "text": rules.answer_text(result)})
    store.save_session(session_id, at, provider, model, turns, result, len(photos), answer["usage"])
    store.log_usage("estimate", provider, model, answer["usage"])
    return {"session_id": session_id, "estimate": result, "provider": provider, "model": model,
            "turns": [turn["text"] for turn in turns if turn["role"] == "user"], "conversation": _conversation(turns, len(photos)),
            "photos": len(photos)}


def _photo_shares(turns: list[dict], total: int) -> list[tuple[int, int]]:
    """Which of a session's photos belong to each turn, as [start, end) indices. A turn stores how many
    photos it added; the first turn of an older session has no count and holds all photos before the
    later turns' ones (photos used to come only with the first message)."""
    later = sum(turn.get("photos") or 0 for turn in turns[1:] if turn["role"] == "user")
    shares, at = [], 0
    for index, turn in enumerate(turns):
        count = 0 if turn["role"] != "user" else (max(0, total - later) if index == 0 else turn.get("photos") or 0)
        shares.append((at, min(at + count, total)))
        at += count
    return shares


def _conversation(messages: list[dict], photos: int = 0) -> list[dict]:
    """The session as the chat shows it: Philipp's words with the indices of the photos he sent with
    them and, for each answer, its carbs with range and reasoning (the stored answer is the JSON
    replayed to the model; links in older answers are dropped, user report 2026-11-19)."""
    rows = []
    for (start, end), turn in zip(_photo_shares(messages, photos), messages):
        if turn["role"] == "user":
            rows.append({"role": "user", "text": "" if turn["text"] == "(nur Fotos)" else turn["text"],
                         "photos": list(range(start, end))})
            continue
        try:
            data = json.loads(turn["text"])
        except (TypeError, ValueError):
            continue
        rows.append({"role": "assistant", "carbs_g": data.get("carbs_g"), "carbs_low_g": data.get("carbs_low_g"),
                     "carbs_high_g": data.get("carbs_high_g"), "reasoning": rules.plain_text(data.get("reasoning")),
                     "question": rules.plain_text(data.get("question"))})
    return rows


def transcribe(payload: dict) -> dict:
    """A voice note as text for the description field (always through OpenRouter)."""
    audio_format = payload.get("format")
    if audio_format not in AUDIO_FORMATS:
        raise ValueError("Unbekanntes Audioformat")
    text = str(payload.get("audio") or "")
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Aufnahme konnte nicht gelesen werden") from None
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise ValueError("Aufnahme ist leer oder zu lang")
    model = _settings()["transcribe_model"]
    try:
        answer = llm.transcribe(model, text, audio_format, TRANSCRIBE_INSTRUCTION)
    except llm.LLMError as error:
        raise ValueError(str(error)) from None
    store.log_usage("transcribe", "openrouter", model, answer["usage"])
    return {"text": answer["text"][:rules.MAX_TEXT]}


def link(session_id, event_id: str) -> None:
    """A saved entry keeps the estimate it came from."""
    try:
        session_id = str(uuid.UUID(str(session_id)))
    except ValueError:
        return
    if store.session(session_id):
        store.link_session(session_id, event_id)


def photo(session_id: str, index: str) -> bytes | None:
    """One photo of an estimate, or None."""
    try:
        path = store.photo_path(str(uuid.UUID(session_id)), int(index))
    except ValueError:
        return None
    return path.read_bytes() if path.is_file() else None


def for_entry(params: dict) -> dict:
    """The newest estimate linked to a diary entry, or the one named by `session_id`, so the chat can
    continue it (user request 2026-11-17: a second plate is told to the agent instead of starting over)."""
    event_id = str((params.get("event_id") or [""])[0])
    session_id = str((params.get("session_id") or [""])[0])
    # An estimate taken over into a form that is not saved yet is continued by its own id (Philipp
    # 2026-11-19: correcting works the same before and after saving).
    if session_id:
        try:
            session = store.session(str(uuid.UUID(session_id)))
        except ValueError:
            session = None
    else:
        session = store.sessions_for([event_id]).get(event_id) if event_id else None
    if not session:
        return {"session": None}
    return {"session": {
        "session_id": session["id"], "photos": session["photos"],
        "estimate": session["estimate"] and {**session["estimate"], "sources": [
            {**source, "url": llm.clean_url(source["url"])} for source in session["estimate"].get("sources") or []]},
        "turns": [turn["text"] for turn in session["messages"] if turn["role"] == "user"],
        "conversation": _conversation(session["messages"], session["photos"]),
        "can_continue": len(session["messages"]) < rules.MAX_TURNS * 2,
    }}


def records(event_ids: list[str]) -> dict[str, dict]:
    """What the read API adds to a meal record: the agent's estimate and its photos."""
    result = {}
    for event_id, session in store.sessions_for(event_ids).items():
        estimate = session["estimate"] or {}
        result[event_id] = {
            "agent_carbs_g": estimate.get("carbs_g"), "agent_carbs_low_g": estimate.get("carbs_low_g"),
            "agent_carbs_high_g": estimate.get("carbs_high_g"),
            "agent_history_adjustment_g": estimate.get("history_adjustment_g"),
            "agent_fat_g": estimate.get("fat_g"), "agent_protein_g": estimate.get("protein_g"),
            "agent_items": estimate.get("items"), "agent_model": session["provider"] + ":" + session["model"],
            "photos": [f"/api/v1/photo?session={session['id']}&n={index}" for index in range(session["photos"])],
        }
    return result
