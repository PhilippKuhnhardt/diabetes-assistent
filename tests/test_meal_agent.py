import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import meal_agent
from app.ingestion import llm
from app.services import agent
from app.store import agent as agent_store, db, dishes as dish_store

ANSWER = {
    "items": [{"name": "Haferflocken", "amount": "50 g", "carbs_g": 29.5}, {"name": "Banane", "amount": "1", "carbs_g": 24}],
    "carbs_g": 58.4, "carbs_low_g": 60, "carbs_high_g": 70, "history_adjustment_g": 5, "certainty": "known",
    "fat_protein": False, "fast": False, "dish_name": "porridge", "dish_category": "Frühstück",
    "description": "Porridge mit Banane", "reasoning": "Bekanntes Gericht.", "question": "",
}


def ns(value):
    """An SDK-like response object: attributes instead of keys, all the way down."""
    if isinstance(value, dict):
        # A tool call's input stays a plain dict, as in the SDKs.
        return SimpleNamespace(**{key: item if key == "input" else ns(item) for key, item in value.items()})
    if isinstance(value, list):
        return [ns(item) for item in value]
    return value


class FakeAPI:
    """Stands in for every SDK client: each create() records its arguments and returns the next answer."""
    def __init__(self, answers):
        self.answers, self.requests = list(answers), []
        self.client = SimpleNamespace(messages=self, responses=self, chat=SimpleNamespace(completions=self))

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return ns(self.answers.pop(0))


JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 100).decode()


class MealAgentRuleTests(unittest.TestCase):
    def test_check_rounds_widens_the_range_and_matches_the_dish_by_name(self):
        catalogue = [{"id": 3, "name": "Porridge", "category": "Frühstück", "hint": "9× eingetragen"}]
        result = meal_agent.check(ANSWER, catalogue)
        self.assertEqual((result["carbs_g"], result["carbs_low_g"], result["carbs_high_g"]), (58, 58, 70))
        self.assertEqual(result["dish"], {"id": 3, "name": "Porridge", "new": False, "category": "Frühstück",
                                          "hint": "9× eingetragen"})
        new = meal_agent.check({**ANSWER, "dish_name": "Kantine Pasta"}, catalogue)["dish"]
        self.assertEqual((new["id"], new["new"], new["category"]), (None, True, "Frühstück"))
        self.assertIsNone(meal_agent.check({**ANSWER, "dish_name": " "}, catalogue)["dish"])
        self.assertEqual(meal_agent.check({**ANSWER, "certainty": "sure"}, [])["certainty"], "unsure")
        with self.assertRaises(ValueError):
            meal_agent.check({**ANSWER, "carbs_g": "viel"}, [])

    def test_the_schema_is_strict_and_the_prompt_names_no_dose(self):
        self.assertEqual(sorted(meal_agent.SCHEMA["required"]), sorted(meal_agent.SCHEMA["properties"]))
        self.assertFalse(meal_agent.SCHEMA["additionalProperties"])
        self.assertIn("nie Insulineinheiten", meal_agent.SYSTEM)

    def test_catalogue_text_lists_outcome_and_recent_entries(self):
        text = meal_agent.catalogue_text(
            [{"id": 3, "name": "Porridge", "category": "Frühstück", "hint": "9× eingetragen, meist 50 g"},
             {"id": 4, "name": "Alt", "category": "", "hint": "", "archived": True}],
            {3: [{"day": "2026-11-15", "carbs_g": 50.0, "description": "50 g Haferflocken"}]})
        self.assertIn("- Porridge [Frühstück]: 9× eingetragen, meist 50 g", text)
        self.assertIn("2026-11-15: 50 g, 50 g Haferflocken", text)
        self.assertNotIn("Alt", text)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a-key", "OPENAI_API_KEY": "o-key",
                                           "OPENROUTER_API_KEY": "r-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = [{"role": "user", "text": "Porridge", "images": [JPEG]}]

    def test_json_is_found_inside_a_fence_or_prose(self):
        self.assertEqual(llm.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.parse_json('Hier: {"a": {"b": 2}} fertig'), {"a": {"b": 2}})
        with self.assertRaises(llm.LLMError):
            llm.parse_json("keine Ahnung")

    def test_anthropic_tool_loop_with_paused_search_turn(self):
        fake = FakeAPI([
            {"stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [
                {"type": "text", "text": "Ich schaue nach."},
                {"type": "tool_use", "id": "t1", "name": "search_meals", "input": {"query": "Reis"}}]},
            {"stop_reason": "pause_turn", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [
                {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "Reis gekocht KH"}},
                {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
                    {"type": "web_search_result", "url": "https://example.org/a", "title": "A"}]}]},
            {"stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 7},
             "content": [{"type": "text", "text": json.dumps(ANSWER)}]},
        ])
        asked = []
        with patch.object(llm, "_client", return_value=fake.client):
            result = llm.run("anthropic", "claude-opus-5-5", "system", self.messages, meal_agent.SCHEMA,
                             meal_agent.TOOLS, lambda name, args: asked.append((name, args)) or '{"total": 0}', True)
        first = fake.requests[0]
        self.assertEqual([tool.get("name") for tool in first["tools"]], ["search_meals", "dish_history", "web_search"])
        self.assertEqual(first["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(first["tools"][2]["type"], "web_search_20260209")
        self.assertNotIn("output_config", first)
        self.assertNotIn("temperature", first)
        self.assertEqual(first["messages"][0]["content"][0]["source"]["media_type"], "image/jpeg")
        # The tool's answer goes back as a tool_result; the paused turn is resent without a new user message.
        self.assertEqual(asked, [("search_meals", {"query": "Reis"})])
        second = fake.requests[1]["messages"]
        self.assertEqual([message["role"] for message in second], ["user", "assistant", "user"])
        self.assertEqual(second[2]["content"], [{"type": "tool_result", "tool_use_id": "t1", "content": '{"total": 0}'}])
        self.assertEqual([message["role"] for message in fake.requests[2]["messages"]], ["user", "assistant", "user", "assistant"])
        self.assertEqual(result["data"]["carbs_g"], 58.4)
        self.assertEqual(result["sources"], [{"url": "https://example.org/a", "title": "A"}])
        self.assertEqual(result["usage"]["input_tokens"], 40)
        self.assertEqual([call["name"] for call in result["tool_calls"]], ["search_meals", "web_search"])
        self.assertEqual(meal_agent.lookups(result["tool_calls"]),
                         ["Frühere Mahlzeiten: „Reis“", "Websuche: „Reis gekocht KH“"])
        # Without web search the schema is enforced; an older model gets the basic search tool.
        done = {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": json.dumps(ANSWER)}]}
        fake = FakeAPI([done, done])
        with patch.object(llm, "_client", return_value=fake.client):
            llm.run("anthropic", "claude-opus-5-5", "system", self.messages, meal_agent.SCHEMA, meal_agent.TOOLS)
            llm.run("anthropic", "claude-haiku-4-5", "system", self.messages, meal_agent.SCHEMA, [], web_search=True)
        self.assertEqual(fake.requests[0]["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(fake.requests[1]["tools"], [{"type": "web_search_20250305", "name": "web_search",
                                                      "max_uses": llm.MAX_SEARCHES}])
        with patch.object(llm, "_client", return_value=FakeAPI([{"stop_reason": "refusal", "content": []}]).client):
            with self.assertRaises(llm.LLMError):
                llm.run("anthropic", "claude-opus-5-5", "system", self.messages, meal_agent.SCHEMA)
        looping = {"stop_reason": "tool_use", "usage": {}, "content": [
            {"type": "tool_use", "id": "t", "name": "dish_history", "input": {"dish": "x"}}]}
        with patch.object(llm, "_client", return_value=FakeAPI([looping] * llm.MAX_TOOL_ROUNDS).client):
            with self.assertRaisesRegex(llm.LLMError, "zu viele"):
                llm.run("anthropic", "claude-opus-5-5", "system", self.messages, meal_agent.SCHEMA,
                        meal_agent.TOOLS, lambda name, args: "{}")

    def test_openai_tool_loop(self):
        fake = FakeAPI([
            {"id": "r1", "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 4}, "output": [
                {"type": "function_call", "call_id": "c1", "name": "dish_history", "arguments": '{"dish": "Porridge"}'}]},
            {"id": "r2", "status": "completed", "usage": {"input_tokens": 5, "output_tokens": 6}, "output": [
                {"type": "web_search_call", "action": {"query": "Haferflocken KH"}},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(ANSWER), "annotations": [
                    {"type": "url_citation", "url": "https://example.org/b", "title": "B"}]}]}]},
        ])
        with patch.object(llm, "_client", return_value=fake.client):
            result = llm.run("openai", "gpt-x", "system", self.messages, meal_agent.SCHEMA, meal_agent.TOOLS,
                             lambda name, args: '{"name": "Porridge"}', True)
        first, second = fake.requests
        self.assertEqual(first["input"][0]["content"][0]["type"], "input_image")
        self.assertTrue(first["text"]["format"]["strict"])
        self.assertEqual([tool["type"] for tool in first["tools"]], ["function", "function", "web_search"])
        self.assertNotIn("previous_response_id", first)
        self.assertEqual(second["previous_response_id"], "r1")
        self.assertEqual(second["input"], [{"type": "function_call_output", "call_id": "c1", "output": '{"name": "Porridge"}'}])
        self.assertEqual(result["sources"][0]["url"], "https://example.org/b")
        self.assertEqual(result["usage"]["output_tokens"], 10)
        self.assertEqual([call["name"] for call in result["tool_calls"]], ["dish_history", "web_search"])

    def test_openrouter_tool_loop_and_transcription(self):
        fake = FakeAPI([
            {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.001}, "choices": [{"finish_reason": "tool_calls",
             "message": {"content": None, "tool_calls": [
                 {"id": "k1", "type": "function", "function": {"name": "search_meals", "arguments": '{"meal": "breakfast"}'}}]}}]},
            {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.002}, "choices": [{"finish_reason": "stop",
             "message": {"content": "```json\n" + json.dumps(ANSWER) + "\n```", "tool_calls": None, "annotations": [
                 {"type": "url_citation", "url_citation": {"url": "https://example.org/c", "title": "C"}}]}}]},
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": [{"finish_reason": "stop",
             "message": {"content": "Porridge mit Banane"}}]},
        ])
        with patch.object(llm, "_client", return_value=fake.client):
            result = llm.run("openrouter", "vendor/model", "system", self.messages, meal_agent.SCHEMA,
                             meal_agent.TOOLS, lambda name, args: '{"total": 3}', True)
            text = llm.transcribe("vendor/audio", "AAAA", "wav", "Transkribiere")
        first, second, audio = fake.requests
        self.assertEqual(first["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(first["extra_body"]["plugins"][0]["id"], "web")
        self.assertEqual(first["tools"][0]["function"]["name"], "search_meals")
        self.assertEqual([message["role"] for message in second["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(second["messages"][3], {"role": "tool", "tool_call_id": "k1", "content": '{"total": 3}'})
        self.assertEqual((round(result["usage"]["cost_usd"], 3), result["sources"][0]["title"]), (0.003, "C"))
        self.assertEqual(audio["messages"][0]["content"][1],
                         {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}})
        self.assertEqual(text["text"], "Porridge mit Banane")

    def test_missing_key_or_model_is_named(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(llm.configured(), ["anthropic", "openrouter"])
            with self.assertRaisesRegex(llm.LLMError, "OPENAI_API_KEY"):
                llm.run("openai", "gpt-x", "s", self.messages, meal_agent.SCHEMA)
        with self.assertRaisesRegex(llm.LLMError, "Modell"):
            llm.run("openrouter", "", "s", self.messages, meal_agent.SCHEMA)


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.old_db = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()
        self.env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a-key", "OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "r-key"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        db.DB_PATH = self.old_db
        self.temp.cleanup()

    def test_settings_name_configured_providers_and_never_a_key(self):
        view = agent.view()
        self.assertEqual((view["provider"], view["available"], view["transcribe_available"]), ("anthropic", True, True))
        self.assertEqual([item["configured"] for item in view["providers"]], [True, False, True])
        self.assertNotIn("a-key", json.dumps(view))
        with self.assertRaises(ValueError):
            agent.save_settings({"provider": "openai"})
        view = agent.save_settings({"provider": "openrouter", "model_openrouter": "vendor/model", "web_search": False})
        self.assertEqual((view["provider"], view["web_search"], view["available"]), ("openrouter", False, True))

    def test_estimate_stores_session_photos_and_usage_and_a_follow_up_replays_them(self):
        dish_store.create_dish("Porridge", "Frühstück")
        answer = {"data": ANSWER, "sources": [], "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": None},
                  "tool_calls": [{"name": "dish_history", "args": {"dish": "Porridge"}}]}
        with patch.object(llm, "run", return_value=answer) as complete:
            first = agent.estimate({"text": "Porridge wie immer", "photos": ["data:image/jpeg;base64," + JPEG],
                                    "at": "2026-11-16T07:30:00+00:00"})
            provider, model, system, messages, schema, tools, call_tool, web = complete.call_args.args
            self.assertEqual((provider, model, web), ("anthropic", "claude-opus-5-5", True))
            self.assertEqual(tools, meal_agent.TOOLS)
            self.assertEqual(json.loads(call_tool("dish_history", {"dish": "porridge"}))["name"], "Porridge")
            self.assertIn("Unbekanntes Gericht", call_tool("dish_history", {"dish": "Pizza"}))
            self.assertEqual(json.loads(call_tool("search_meals", {"query": "Reis"}))["entries"], [])
            self.assertIn("error", json.loads(call_tool("dish_history", {"dish": "x", "days": "viele"})))
            self.assertIn("error", json.loads(call_tool("rm", {})))
            self.assertIn("- Porridge [Frühstück]", messages[0]["text"])
            self.assertIn("Philipp: Porridge wie immer", messages[0]["text"])
            self.assertEqual(len(messages[0]["images"]), 1)
            self.assertEqual(first["estimate"]["dish"]["new"], False)
            self.assertEqual(first["estimate"]["lookups"], ["Verlauf: Porridge"])
            second = agent.estimate({"session_id": first["session_id"], "text": "mit 2 Bananen"})
            messages = complete.call_args.args[3]
            self.assertEqual([message["role"] for message in messages], ["user", "assistant", "user"])
            self.assertEqual((len(messages[0]["images"]), messages[2]["text"]), (1, "mit 2 Bananen"))
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["turns"], ["Porridge wie immer", "mit 2 Bananen"])
        session = agent_store.session(first["session_id"])
        self.assertEqual((session["calls"], session["input_tokens"], session["photos"]), (2, 200, 1))
        self.assertTrue(agent.photo(first["session_id"], "0").startswith(b"\xff\xd8"))
        self.assertIsNone(agent.photo(first["session_id"], "1"))
        self.assertIsNone(agent.photo("../etc", "0"))
        agent.link(first["session_id"], "11111111-1111-1111-1111-111111111111")
        linked = agent.records(["11111111-1111-1111-1111-111111111111"])["11111111-1111-1111-1111-111111111111"]
        self.assertEqual((linked["agent_carbs_g"], len(linked["photos"])), (58, 1))
        self.assertEqual(agent.view()["usage_month"][0]["calls"], 2)

    def test_estimate_needs_input_and_valid_photos(self):
        for payload in ({}, {"photos": ["data:image/jpeg;base64,AAAA"]}, {"photos": [JPEG] * 5},
                        {"session_id": "nope", "text": "x"}):
            with self.assertRaises(ValueError):
                agent.estimate(payload)
        with patch.object(llm, "run", side_effect=llm.LLMError("Anbieter lehnt ab (402, kein Guthaben): x")):
            with self.assertRaisesRegex(ValueError, "kein Guthaben"):
                agent.estimate({"text": "Brot"})


if __name__ == "__main__":
    unittest.main()
