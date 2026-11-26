"""LLM providers for the meal agent (user request 2026-11-16): Claude (Anthropic), ChatGPT (OpenAI)
and OpenRouter, so credits of any of them can be used. Keys come from the deployment environment
(ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY) and never reach the page or a log.

The calls go through the vendors' SDKs (`anthropic`, `openai`; OpenRouter speaks the OpenAI chat
API). Every provider runs the same agent loop: the model gets the conversation (text and JPEG
photos), the app's lookup tools and, if allowed, the provider's own web search; it may call tools as
often as it finds useful (up to MAX_TOOL_ROUNDS), and its final answer is a JSON object. Philipp's
philosophy (2026-11-16): the model is smart, so it gets tools to decide well rather than rules.

`run` returns {"data": parsed JSON, "sources": [{url, title}], "usage": {input_tokens, output_tokens,
cost_usd}, "tool_calls": [{name, args}]}."""
from __future__ import annotations

import json
import os
import re
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anthropic
import openai
from app.i18n import tr

PROVIDERS = {
    "anthropic": {"label": "Claude (Anthropic)", "key": "ANTHROPIC_API_KEY", "default_model": "claude-opus-5-5"},
    "openai": {"label": "ChatGPT (OpenAI)", "key": "OPENAI_API_KEY", "default_model": ""},
    "openrouter": {"label": "OpenRouter", "key": "OPENROUTER_API_KEY", "default_model": ""},
}
OPENROUTER_URL = "https://openrouter.ai/api/v1"
# Claude models that take the web search tool with dynamic filtering; older ones use the basic tool.
ANTHROPIC_NEW_SEARCH = re.compile(r"fable|mythos|opus-5|opus-4-[678]|sonnet-5|sonnet-4-6")
TIMEOUT_SECONDS = 180
MAX_OUTPUT_TOKENS = 16000
MAX_SEARCHES = 10
# Model turns per answer: each round may call several tools; a paused search turn counts as one.
MAX_TOOL_ROUNDS = 12

ToolCall = Callable[[str, dict], str]


class LLMError(Exception):
    """A provider call failed; the message is safe to show (never holds a key)."""


def api_key(provider: str) -> str:
    return os.environ.get(PROVIDERS[provider]["key"], "").strip()


def configured() -> list[str]:
    """Providers whose key is set in the deployment."""
    return [name for name in PROVIDERS if api_key(name)]


def _client(provider: str):
    """The SDK client for a provider (tests replace this)."""
    if provider == "anthropic":
        return anthropic.Anthropic(api_key=api_key("anthropic"), timeout=TIMEOUT_SECONDS, max_retries=2)
    if provider == "openai":
        return openai.OpenAI(api_key=api_key("openai"), timeout=TIMEOUT_SECONDS, max_retries=2)
    return openai.OpenAI(api_key=api_key("openrouter"), base_url=OPENROUTER_URL, timeout=TIMEOUT_SECONDS,
                         max_retries=2, default_headers={"X-Title": "Diabetes-Tagebuch"})


HINTS = {401: tr("Schlüssel ungültig"), 402: tr("kein Guthaben"), 403: tr("Zugriff verweigert"),
         404: tr("Modell nicht gefunden"), 429: tr("Limit erreicht, später erneut versuchen")}


def _guard(call: Callable):
    """Run one SDK call; its errors become an LLMError the page can show."""
    try:
        return call()
    except (anthropic.APIStatusError, openai.APIStatusError) as error:
        hint = HINTS.get(error.status_code, "")
        raise LLMError(tr("Anbieter lehnt ab ({status}): {message}", status=f"{error.status_code}{', ' + hint if hint else ''}",
                          message=str(getattr(error, "message", "") or "")[:300])) from None
    except (anthropic.APIConnectionError, openai.APIConnectionError) as error:
        raise LLMError(tr("Anbieter nicht erreichbar: {error}", error=type(error).__name__)) from None


def parse_json(text: str) -> dict:
    """The JSON object of an answer, also when the model wrapped it in a code fence or prose."""
    text = str(text or "").strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError(tr("Antwort des Modells war kein JSON")) from None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            raise LLMError(tr("Antwort des Modells war kein JSON")) from None
    if not isinstance(value, dict):
        raise LLMError(tr("Antwort des Modells war kein JSON-Objekt"))
    return value


def _field(item, name: str, default=None):
    """A field of an SDK object or of a plain dict (OpenRouter extras arrive as dicts)."""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def clean_url(url: str) -> str:
    """A source link without tracking parameters (utm_*, e.g. utm_source=openai that OpenAI's search adds)."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _sources(found: list[tuple[str, str]]) -> list[dict]:
    seen, result = set(), []
    for url, title in found:
        url = clean_url(url) if isinstance(url, str) else url
        if isinstance(url, str) and url.startswith("http") and url not in seen:
            seen.add(url)
            result.append({"url": url[:500], "title": str(title or "")[:200]})
    return result[:10]


def _arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _anthropic(model, system, messages, schema, tools, call_tool, web_search) -> dict:
    client = _client("anthropic")

    def content(message: dict):
        parts = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}}
                 for image in message.get("images", [])]
        return parts + [{"type": "text", "text": message["text"]}]

    params = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "system": system,
              "tools": [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
                        for tool in tools]}
    if web_search:
        params["tools"].append({"type": "web_search_20260209" if ANTHROPIC_NEW_SEARCH.search(model)
                                else "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES})
    else:
        # Structured output guarantees the schema; with web search (citations) the answer is parsed from text.
        params["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    conversation = [{"role": message["role"], "content": content(message)} for message in messages]
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    found, calls = [], []
    for _ in range(MAX_TOOL_ROUNDS):
        response = _guard(lambda: client.messages.create(messages=conversation, **params))
        usage["input_tokens"] += int(_field(_field(response, "usage"), "input_tokens", 0) or 0)
        usage["output_tokens"] += int(_field(_field(response, "usage"), "output_tokens", 0) or 0)
        for block in response.content:
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                found += [(item.url, item.title) for item in block.content if item.type == "web_search_result"]
            if block.type == "server_tool_use":
                calls.append({"name": block.name, "args": _arguments(block.input)})
        if response.stop_reason == "pause_turn":
            # The provider's search loop paused: resend with its partial turn, it resumes by itself.
            conversation.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":
            break
        conversation.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                args = _arguments(block.input)
                calls.append({"name": block.name, "args": args})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": call_tool(block.name, args)})
        conversation.append({"role": "user", "content": results})
    else:
        raise LLMError(tr("Das Modell hat zu viele Nachschlage-Runden gebraucht"))
    if response.stop_reason == "refusal":
        raise LLMError(tr("Das Modell hat die Anfrage abgelehnt"))
    if response.stop_reason == "max_tokens":
        raise LLMError(tr("Antwort des Modells wurde abgeschnitten"))
    text = "".join(block.text for block in response.content if block.type == "text")
    return {"data": parse_json(text), "sources": _sources(found), "usage": usage, "tool_calls": calls}


def _openai(model, system, messages, schema, tools, call_tool, web_search) -> dict:
    client = _client("openai")

    def content(message: dict):
        if message["role"] == "assistant":
            return message["text"]
        return [{"type": "input_image", "image_url": "data:image/jpeg;base64," + image}
                for image in message.get("images", [])] + [{"type": "input_text", "text": message["text"]}]

    params = {"model": model, "instructions": system, "max_output_tokens": MAX_OUTPUT_TOKENS,
              "tools": [{"type": "function", "name": tool["name"], "description": tool["description"],
                         "parameters": tool["parameters"], "strict": False} for tool in tools]
                       + ([{"type": "web_search"}] if web_search else []),
              "text": {"format": {"type": "json_schema", "name": "meal_estimate", "schema": schema, "strict": True}}}
    current = [{"role": message["role"], "content": content(message)} for message in messages]
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    calls, previous = [], None
    for _ in range(MAX_TOOL_ROUNDS):
        response = _guard(lambda: client.responses.create(
            input=current, **({"previous_response_id": previous} if previous else {}), **params))
        usage["input_tokens"] += int(_field(_field(response, "usage"), "input_tokens", 0) or 0)
        usage["output_tokens"] += int(_field(_field(response, "usage"), "output_tokens", 0) or 0)
        pending = [item for item in response.output if item.type == "function_call"]
        calls += [{"name": "web_search", "args": {"query": _field(_field(item, "action"), "query", "")}}
                  for item in response.output if item.type == "web_search_call"]
        if not pending:
            break
        current = []
        for item in pending:
            args = _arguments(item.arguments)
            calls.append({"name": item.name, "args": args})
            current.append({"type": "function_call_output", "call_id": item.call_id, "output": call_tool(item.name, args)})
        previous = response.id
    else:
        raise LLMError(tr("Das Modell hat zu viele Nachschlage-Runden gebraucht"))
    if response.status == "incomplete":
        raise LLMError(tr("Antwort des Modells wurde abgeschnitten"))
    text, found = "", []
    for item in response.output:
        for part in (item.content or []) if item.type == "message" else []:
            if part.type == "refusal":
                raise LLMError(tr("Das Modell hat die Anfrage abgelehnt"))
            if part.type == "output_text":
                text += part.text
                found += [(note.url, note.title) for note in part.annotations or [] if note.type == "url_citation"]
    return {"data": parse_json(text), "sources": _sources(found), "usage": usage, "tool_calls": calls}


def _openrouter_usage(response) -> dict:
    usage = _field(response, "usage")
    cost = _field(usage, "cost")
    return {"input_tokens": int(_field(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(_field(usage, "completion_tokens", 0) or 0),
            "cost_usd": float(cost) if isinstance(cost, (int, float)) else None}


def _openrouter_message(response):
    if _field(response, "error"):
        raise LLMError("OpenRouter: " + str(_field(response.error, "message", "") or "")[:300])
    if not response.choices:
        raise LLMError(tr("OpenRouter lieferte keine Antwort"))
    return response.choices[0].message


def _openrouter(model, system, messages, schema, tools, call_tool, web_search) -> dict:
    client = _client("openrouter")

    def content(message: dict):
        if message["role"] == "assistant" or not message.get("images"):
            return message["text"]
        return [{"type": "text", "text": message["text"]}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}
            for image in message["images"]]

    # Not every OpenRouter model takes a response format together with tools, so the answer's JSON
    # is asked for in the system text and parsed.
    params = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS,
              "tools": [{"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                          "parameters": tool["parameters"]}} for tool in tools],
              "extra_body": {"usage": {"include": True},
                             **({"plugins": [{"id": "web", "max_results": MAX_SEARCHES}]} if web_search else {})}}
    conversation = [{"role": "system", "content": system}] + [
        {"role": message["role"], "content": content(message)} for message in messages]
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    calls, found = [], []
    for _ in range(MAX_TOOL_ROUNDS):
        response = _guard(lambda: client.chat.completions.create(messages=conversation, **params))
        step = _openrouter_usage(response)
        usage["input_tokens"] += step["input_tokens"]
        usage["output_tokens"] += step["output_tokens"]
        if step["cost_usd"] is not None:
            usage["cost_usd"] = (usage["cost_usd"] or 0) + step["cost_usd"]
        message = _openrouter_message(response)
        for note in _field(message, "annotations") or []:
            if _field(note, "type") == "url_citation":
                citation = _field(note, "url_citation") or {}
                found.append((_field(citation, "url"), _field(citation, "title")))
        pending = _field(message, "tool_calls") or []
        if not pending:
            break
        conversation.append({"role": "assistant", "content": _field(message, "content") or "",
                             "tool_calls": [{"id": call.id, "type": "function",
                                             "function": {"name": call.function.name,
                                                          "arguments": call.function.arguments or "{}"}}
                                            for call in pending]})
        for call in pending:
            args = _arguments(call.function.arguments)
            calls.append({"name": call.function.name, "args": args})
            conversation.append({"role": "tool", "tool_call_id": call.id, "content": call_tool(call.function.name, args)})
    else:
        raise LLMError(tr("Das Modell hat zu viele Nachschlage-Runden gebraucht"))
    if response.choices[0].finish_reason == "length":
        raise LLMError(tr("Antwort des Modells wurde abgeschnitten"))
    text = _field(message, "content")
    if isinstance(text, list):
        text = "".join(_field(part, "text", "") for part in text)
    return {"data": parse_json(text), "sources": _sources(found), "usage": usage, "tool_calls": calls}


def run(provider: str, model: str, system: str, messages: list[dict], schema: dict,
        tools: list[dict] | None = None, call_tool: ToolCall | None = None, web_search: bool = False) -> dict:
    """The agent's answer as JSON matching `schema`. messages: [{"role": "user"|"assistant", "text": str,
    "images": [base64 JPEG, ...]}], ending with a user message. tools: [{name, description, parameters
    (JSON schema)}]; call_tool(name, args) answers a call with text (JSON) and never raises."""
    if provider not in PROVIDERS:
        raise LLMError(tr("Unbekannter Anbieter"))
    if not api_key(provider):
        raise LLMError(tr("{key} ist in der Bereitstellung nicht gesetzt", key=PROVIDERS[provider]["key"]))
    if not model:
        raise LLMError(tr("Bitte in den Einstellungen ein Modell wählen"))
    return {"anthropic": _anthropic, "openai": _openai, "openrouter": _openrouter}[provider](
        model, system, messages, schema, tools or [], call_tool or (lambda name, args: '{"error": "unbekannt"}'),
        web_search)


def transcribe(model: str, audio_base64: str, audio_format: str, instruction: str) -> dict:
    """Speech to text through OpenRouter with a model that takes audio (user request 2026-11-16)."""
    if not api_key("openrouter"):
        raise LLMError(tr("OPENROUTER_API_KEY ist in der Bereitstellung nicht gesetzt"))
    if not model:
        raise LLMError(tr("Bitte in den Einstellungen ein Modell für die Transkription wählen"))
    client = _client("openrouter")
    response = _guard(lambda: client.chat.completions.create(
        model=model, max_tokens=2000, extra_body={"usage": {"include": True}},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": instruction},
            {"type": "input_audio", "input_audio": {"data": audio_base64, "format": audio_format}}]}]))
    text = _field(_openrouter_message(response), "content")
    if isinstance(text, list):
        text = "".join(_field(part, "text", "") for part in text)
    return {"text": str(text or "").strip(), "usage": _openrouter_usage(response)}


def models(provider: str) -> list[dict]:
    """The provider's model list for the settings: [{id, name, image, audio}]; image/audio are None
    where the provider does not say what a model takes."""
    if provider not in PROVIDERS or not api_key(provider):
        raise LLMError(tr("Anbieter ist nicht eingerichtet"))
    client = _client(provider)
    rows = list(_guard(lambda: client.models.list(**({"limit": 100} if provider == "anthropic" else {}))))
    if provider == "anthropic":
        return [{"id": row.id, "name": _field(row, "display_name") or row.id, "image": True, "audio": False}
                for row in rows if row.id]
    if provider == "openai":
        skip = ("embedding", "whisper", "tts", "dall-e", "image", "audio", "realtime", "moderation", "transcribe",
                "babbage", "davinci", "sora", "codex", "search")
        ids = sorted({row.id for row in rows if row.id and not any(word in row.id for word in skip)}, reverse=True)
        return [{"id": model, "name": model, "image": None, "audio": None} for model in ids]
    result = []
    for row in rows:
        takes = _field(_field(row, "architecture") or {}, "input_modalities") or []
        if row.id:
            result.append({"id": row.id, "name": _field(row, "name") or row.id,
                           "image": "image" in takes, "audio": "audio" in takes})
    return sorted(result, key=lambda row: row["id"])
