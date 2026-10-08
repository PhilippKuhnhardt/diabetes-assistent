"""Model Context Protocol server (Streamable HTTP, JSON responses only) for a conversational agent.

One POST /mcp carries one JSON-RPC message; the answer is plain JSON (no event stream, no server
sessions). The tools live in app/services/agent_tools.py and reuse the entry form's services.
"""
from __future__ import annotations

import json

from app.common import json_safe
from app.ingestion import nightscout
from app.services import agent_tools

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "diabetes-assistent", "title": "Diabetes-Tagebuch", "version": "1"}
INSTRUCTIONS = """Du sprichst mit Philipp (Typ-1-Diabetes, FreeStyle Libre 3 Plus, Fiasp per Pen) über sein \
Diabetes-Tagebuch. Antworte auf Deutsch, kurz und gesprochen gut verständlich.

- Werte, Faktoren und Dosisvorschläge kommen nur aus den Tools. Rechne nie selbst eine Dosis aus und runde \
nichts um. Die Vorschläge sind Modellrechnungen der App (Annahmen: volle Wirkung bis zum Endpunkt, keine \
Drift), keine ärztliche Anweisung; Philipp entscheidet.
- "Wie läuft es?": get_status. Nenne BZ, Trend, was noch wirkt und den erwarteten Wert.
- Mahlzeit: preview_entry mit den KH. Nenne die Optionen (normal, vorsichtig, nur KH; bei Split den Teil \
jetzt und den 2. Teil mit Uhrzeit) und den Faktor. Weise hin, wenn die App den Eintrag als Snack einer \
Mahlzeit einordnet, Sport vor der Mahlzeit gefunden hat (Einfluss erfragen) oder Basal heute noch fehlt \
(fragen, ob mit eintragen).
- Speichern (save_entry) erst, wenn Philipp die gegessenen KH und die tatsächlich gespritzten IE bestätigt \
hat. Bei KH immer KH-Sicherheit (gewogen / halbwegs sicher / unsicher) und fett-/eiweißreich mitgeben; \
fehlt die Angabe, kurz nachfragen. dose_variant = gewählte Option. Zeit weglassen (= jetzt), außer Philipp \
nennt bewusst eine andere.
- Korrektur ohne Essen: preview_entry ohne KH, dann save_entry nur mit insulin_units. Offener 2. Split-Teil \
erscheint als Option split.
- Nach dem Speichern kurz bestätigen, was eingetragen wurde. Falsch verstanden: delete_entry nur auf \
ausdrückliche Bitte, danach neu eintragen.
- Bei Fehlern nicht mit anderen Werten erneut speichern; Meldung vorlesen und nachfragen. Wiederholung nach \
Timeout mit derselben event_id."""


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _tool(name: str) -> dict:
    tool = next((item for item in agent_tools.TOOLS if item["name"] == name), None)
    if tool is None:
        raise RpcError(-32602, f"Unbekanntes Tool: {name}")
    return tool


def _call(params: dict) -> dict:
    tool = _tool(str(params.get("name")))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError(-32602, "arguments muss ein Objekt sein")
    try:
        result = json_safe(tool["handler"](arguments))
    except (ValueError, KeyError, nightscout.NightscoutError) as error:
        # Tool errors go back to the agent as a result it can read, not as a protocol error.
        message = str(error) or "Ungültige Eingabe"
        return {"content": [{"type": "text", "text": message}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result, "isError": False}


def _dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {"protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{key: value for key, value in tool.items() if key != "handler"}
                          for tool in agent_tools.TOOLS]}
    if method == "tools/call":
        return _call(params)
    raise RpcError(-32601, f"Unbekannte Methode: {method}")


def handle(message: object) -> dict | None:
    """Answer one JSON-RPC message; notifications and responses from the client get None (HTTP 202)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Ungültige Anfrage"}}
    if "method" not in message or "id" not in message:
        return None
    request_id = message["id"]
    params = message.get("params") or {}
    try:
        if not isinstance(params, dict):
            raise RpcError(-32602, "params muss ein Objekt sein")
        return {"jsonrpc": "2.0", "id": request_id, "result": _dispatch(str(message["method"]), params)}
    except RpcError as error:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": error.code, "message": str(error)}}
    except Exception:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Interner Fehler"}}
