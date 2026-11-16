"""LLM providers for the meal agent (user request 2026-11-16): Claude (Anthropic), ChatGPT (OpenAI)
and OpenRouter over plain HTTPS, so credits of any of them can be used. Keys come from the deployment
environment (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY) and never reach the page or a log.

Every provider takes the same request (system text, a conversation of text and JPEG images, a JSON
schema for the answer, optional web search by the provider's own search tool) and returns the same
shape: {"data": parsed JSON, "sources": [{url, title}], "usage": {input_tokens, output_tokens, cost_usd}}.
The app has no third-party packages, so the calls are raw HTTP (no vendor SDK)."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

PROVIDERS = {
    "anthropic": {"label": "Claude (Anthropic)", "key": "ANTHROPIC_API_KEY", "default_model": "claude-opus-5-5"},
    "openai": {"label": "ChatGPT (OpenAI)", "key": "OPENAI_API_KEY", "default_model": ""},
    "openrouter": {"label": "OpenRouter", "key": "OPENROUTER_API_KEY", "default_model": ""},
}
ANTHROPIC_VERSION = "2026-10-21"
# Claude models that take the web search tool with dynamic filtering; older ones use the basic tool.
ANTHROPIC_NEW_SEARCH = re.compile(r"fable|mythos|opus-5|opus-4-[678]|sonnet-5|sonnet-4-6")
# Claude models with server-side fallbacks: a safety-classifier decline is re-run on another model.
ANTHROPIC_FALLBACKS = re.compile(r"fable-5-[12]|opus-5|sonnet-5-5")
TIMEOUT_SECONDS = 180
MAX_OUTPUT_TOKENS = 16000
MAX_SEARCHES = 4
# A turn the provider paused inside its own search loop is resumed at most this often.
MAX_CONTINUATIONS = 3


class LLMError(Exception):
    """A provider call failed; the message is safe to show (never holds a key)."""


def api_key(provider: str) -> str:
    return os.environ.get(PROVIDERS[provider]["key"], "").strip()


def configured() -> list[str]:
    """Providers whose key is set in the deployment."""
    return [name for name in PROVIDERS if api_key(name)]


def _call(url: str, headers: dict, body: dict | None = None, timeout: int = TIMEOUT_SECONDS) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        # Providers explain a rejected request in the body's error message; it never echoes the key.
        try:
            detail = json.loads(error.read() or b"{}").get("error") or {}
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
        except ValueError:
            message = ""
        hint = {401: "Schlüssel ungültig", 402: "kein Guthaben", 403: "Zugriff verweigert",
                404: "Modell nicht gefunden", 429: "Limit erreicht, später erneut versuchen"}.get(error.code, "")
        raise LLMError(f"Anbieter lehnt ab ({error.code}{', ' + hint if hint else ''}): {str(message or '')[:300]}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise LLMError(f"Anbieter nicht erreichbar: {type(error).__name__}") from None


def parse_json(text: str) -> dict:
    """The JSON object of an answer, also when the model wrapped it in a code fence or prose."""
    text = str(text or "").strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("Antwort des Modells war kein JSON") from None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            raise LLMError("Antwort des Modells war kein JSON") from None
    if not isinstance(value, dict):
        raise LLMError("Antwort des Modells war kein JSON-Objekt")
    return value


def _sources(found: list[tuple[str, str]]) -> list[dict]:
    seen, result = set(), []
    for url, title in found:
        if isinstance(url, str) and url.startswith("http") and url not in seen:
            seen.add(url)
            result.append({"url": url[:500], "title": str(title or "")[:200]})
    return result[:8]


def _anthropic(model: str, system: str, messages: list[dict], schema: dict, web_search: bool) -> dict:
    def content(message: dict):
        parts = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}}
                 for image in message.get("images", [])]
        return parts + [{"type": "text", "text": message["text"]}]

    body = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "system": system,
            "messages": [{"role": message["role"], "content": content(message)} for message in messages]}
    headers = {"x-api-key": api_key("anthropic"), "anthropic-version": ANTHROPIC_VERSION}
    if ANTHROPIC_FALLBACKS.search(model):
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    if web_search:
        body["tools"] = [{"type": "web_search_20260209" if ANTHROPIC_NEW_SEARCH.search(model) else "web_search_20250305",
                          "name": "web_search", "max_uses": MAX_SEARCHES}]
    else:
        # Structured output guarantees the schema; with web search the answer is parsed from its text.
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    found = []
    for _ in range(MAX_CONTINUATIONS + 1):
        answer = _call("https://api.anthropic.com/v1/messages", headers, body)
        for key in ("input_tokens", "output_tokens"):
            usage[key] += int((answer.get("usage") or {}).get(key) or 0)
        blocks = answer.get("content") or []
        for block in blocks:
            if block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), list):
                found += [(item.get("url"), item.get("title")) for item in block["content"]
                          if item.get("type") == "web_search_result"]
        if answer.get("stop_reason") != "pause_turn":
            break
        # The provider's search loop paused: resend with its partial turn, it resumes by itself.
        body["messages"] = body["messages"] + [{"role": "assistant", "content": blocks}]
    if answer.get("stop_reason") == "refusal":
        raise LLMError("Das Modell hat die Anfrage abgelehnt")
    if answer.get("stop_reason") == "max_tokens":
        raise LLMError("Antwort des Modells wurde abgeschnitten")
    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    return {"data": parse_json(text), "sources": _sources(found), "usage": usage}


def _openai(model: str, system: str, messages: list[dict], schema: dict, web_search: bool) -> dict:
    def content(message: dict):
        if message["role"] == "assistant":
            return message["text"]
        return [{"type": "input_image", "image_url": "data:image/jpeg;base64," + image}
                for image in message.get("images", [])] + [{"type": "input_text", "text": message["text"]}]

    body = {"model": model, "instructions": system, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "input": [{"role": message["role"], "content": content(message)} for message in messages],
            "text": {"format": {"type": "json_schema", "name": "meal_estimate", "schema": schema, "strict": True}}}
    if web_search:
        body["tools"] = [{"type": "web_search"}]
    answer = _call("https://api.openai.com/v1/responses", {"Authorization": "Bearer " + api_key("openai")}, body)
    if answer.get("status") == "incomplete":
        raise LLMError("Antwort des Modells wurde abgeschnitten")
    text, found = "", []
    for item in answer.get("output") or []:
        for part in (item.get("content") or []) if item.get("type") == "message" else []:
            if part.get("type") == "refusal":
                raise LLMError("Das Modell hat die Anfrage abgelehnt")
            if part.get("type") == "output_text":
                text += part.get("text", "")
                found += [(note.get("url"), note.get("title")) for note in part.get("annotations") or []
                          if note.get("type") == "url_citation"]
    usage = answer.get("usage") or {}
    return {"data": parse_json(text), "sources": _sources(found),
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0),
                      "output_tokens": int(usage.get("output_tokens") or 0), "cost_usd": None}}


def _openrouter_call(body: dict) -> dict:
    answer = _call("https://openrouter.ai/api/v1/chat/completions",
                   {"Authorization": "Bearer " + api_key("openrouter"), "X-Title": "Diabetes-Tagebuch"}, body)
    if answer.get("error"):
        raise LLMError("OpenRouter: " + str((answer["error"] or {}).get("message") or "")[:300])
    choices = answer.get("choices") or []
    if not choices:
        raise LLMError("OpenRouter lieferte keine Antwort")
    return answer


def _openrouter_usage(answer: dict) -> dict:
    usage = answer.get("usage") or {}
    return {"input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cost_usd": float(usage["cost"]) if isinstance(usage.get("cost"), (int, float)) else None}


def _openrouter(model: str, system: str, messages: list[dict], schema: dict, web_search: bool) -> dict:
    def content(message: dict):
        if message["role"] == "assistant" or not message.get("images"):
            return message["text"]
        return [{"type": "text", "text": message["text"]}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}
            for image in message["images"]]

    body = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "usage": {"include": True},
            "messages": [{"role": "system", "content": system}]
                        + [{"role": message["role"], "content": content(message)} for message in messages]}
    if web_search:
        body["plugins"] = [{"id": "web", "max_results": MAX_SEARCHES}]
    answer = _openrouter_call(body)
    message = answer["choices"][0].get("message") or {}
    found = [((note.get("url_citation") or {}).get("url"), (note.get("url_citation") or {}).get("title"))
             for note in message.get("annotations") or [] if note.get("type") == "url_citation"]
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return {"data": parse_json(text), "sources": _sources(found), "usage": _openrouter_usage(answer)}


def complete(provider: str, model: str, system: str, messages: list[dict], schema: dict,
             web_search: bool = False) -> dict:
    """One answer as JSON matching `schema`. messages: [{"role": "user"|"assistant", "text": str,
    "images": [base64 JPEG, ...]}], ending with a user message."""
    if provider not in PROVIDERS:
        raise LLMError("Unbekannter Anbieter")
    if not api_key(provider):
        raise LLMError(f"{PROVIDERS[provider]['key']} ist in der Bereitstellung nicht gesetzt")
    if not model:
        raise LLMError("Bitte in den Einstellungen ein Modell wählen")
    return {"anthropic": _anthropic, "openai": _openai, "openrouter": _openrouter}[provider](
        model, system, messages, schema, web_search)


def transcribe(model: str, audio_base64: str, audio_format: str, instruction: str) -> dict:
    """Speech to text through OpenRouter with a model that takes audio (user request 2026-11-16)."""
    if not api_key("openrouter"):
        raise LLMError("OPENROUTER_API_KEY ist in der Bereitstellung nicht gesetzt")
    if not model:
        raise LLMError("Bitte in den Einstellungen ein Modell für die Transkription wählen")
    answer = _openrouter_call({
        "model": model, "max_tokens": 2000, "usage": {"include": True},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": instruction},
            {"type": "input_audio", "input_audio": {"data": audio_base64, "format": audio_format}}]}]})
    text = (answer["choices"][0].get("message") or {}).get("content")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return {"text": str(text or "").strip(), "usage": _openrouter_usage(answer)}


def models(provider: str) -> list[dict]:
    """The provider's model list for the settings: [{id, name, image, audio}]; image/audio are None
    where the provider does not say what a model takes."""
    if provider not in PROVIDERS or not api_key(provider):
        raise LLMError("Anbieter ist nicht eingerichtet")
    if provider == "anthropic":
        rows = _call("https://api.anthropic.com/v1/models?limit=100",
                     {"x-api-key": api_key("anthropic"), "anthropic-version": ANTHROPIC_VERSION}, timeout=20).get("data") or []
        return [{"id": row["id"], "name": row.get("display_name") or row["id"], "image": True, "audio": False}
                for row in rows if row.get("id")]
    if provider == "openai":
        rows = _call("https://api.openai.com/v1/models", {"Authorization": "Bearer " + api_key("openai")},
                     timeout=20).get("data") or []
        skip = ("embedding", "whisper", "tts", "dall-e", "image", "audio", "realtime", "moderation", "transcribe",
                "babbage", "davinci", "sora", "codex", "search")
        ids = sorted({row["id"] for row in rows if row.get("id") and not any(word in row["id"] for word in skip)},
                     reverse=True)
        return [{"id": model, "name": model, "image": None, "audio": None} for model in ids]
    rows = _call("https://openrouter.ai/api/v1/models", {"Authorization": "Bearer " + api_key("openrouter")},
                 timeout=20).get("data") or []
    result = []
    for row in rows:
        takes = (row.get("architecture") or {}).get("input_modalities") or []
        if row.get("id"):
            result.append({"id": row["id"], "name": row.get("name") or row["id"],
                           "image": "image" in takes, "audio": "audio" in takes})
    return sorted(result, key=lambda row: row["id"])
