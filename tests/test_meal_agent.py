import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
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

    def test_anthropic_request_and_paused_search_turn(self):
        answers = [
            {"stop_reason": "pause_turn", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [
                {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
                {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
                    {"type": "web_search_result", "url": "https://example.org/a", "title": "A"}]}]},
            {"stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 7},
             "content": [{"type": "text", "text": json.dumps(ANSWER)}]},
        ]
        calls = []
        with patch.object(llm, "_call", side_effect=lambda url, headers, body=None, timeout=0:
                          calls.append((url, headers, json.loads(json.dumps(body)))) or answers[len(calls) - 1]):
            result = llm.complete("anthropic", "claude-opus-5-5", "system", self.messages, meal_agent.SCHEMA, True)
        url, headers, body = calls[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual((headers["x-api-key"], headers["anthropic-version"]), ("a-key", "2026-10-21"))
        self.assertEqual(body["tools"][0]["type"], "web_search_20260209")
        self.assertEqual(body["fallbacks"], "default")
        self.assertNotIn("output_config", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["messages"][0]["content"][0]["source"]["media_type"], "image/jpeg")
        # The paused turn is resent with the assistant's partial content and no extra user message.
        self.assertEqual([message["role"] for message in calls[1][2]["messages"]], ["user", "assistant"])
        self.assertEqual(result["data"]["carbs_g"], 58.4)
        self.assertEqual(result["sources"], [{"url": "https://example.org/a", "title": "A"}])
        self.assertEqual(result["usage"]["input_tokens"], 30)
        # Without web search the schema is enforced; an older model gets the basic search tool and no fallbacks.
        with patch.object(llm, "_call", return_value=answers[1]) as call:
            llm.complete("anthropic", "claude-opus-5-5", "system", self.messages, meal_agent.SCHEMA, False)
            self.assertEqual(call.call_args.args[2]["output_config"]["format"]["type"], "json_schema")
            llm.complete("anthropic", "claude-haiku-4-5", "system", self.messages, meal_agent.SCHEMA, True)
            self.assertEqual(call.call_args.args[2]["tools"][0]["type"], "web_search_20250305")
            self.assertNotIn("fallbacks", call.call_args.args[2])
        with patch.object(llm, "_call", return_value={"stop_reason": "refusal", "content": []}):
            with self.assertRaises(llm.LLMError):
                llm.complete("anthropic", "claude-opus-5-5", "system", self.messages, meal_agent.SCHEMA, False)

    def test_openai_and_openrouter_requests(self):
        openai = {"status": "completed", "usage": {"input_tokens": 3, "output_tokens": 4}, "output": [
            {"type": "web_search_call"},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(ANSWER), "annotations": [
                {"type": "url_citation", "url": "https://example.org/b", "title": "B"}]}]}]}
        with patch.object(llm, "_call", return_value=openai) as call:
            result = llm.complete("openai", "gpt-x", "system", self.messages, meal_agent.SCHEMA, True)
        url, headers, body = call.call_args.args
        self.assertEqual((url, headers["Authorization"]), ("https://api.openai.com/v1/responses", "Bearer o-key"))
        self.assertEqual(body["input"][0]["content"][0]["type"], "input_image")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(result["sources"][0]["url"], "https://example.org/b")
        router = {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.002}, "choices": [{"message": {
            "content": "```json\n" + json.dumps(ANSWER) + "\n```",
            "annotations": [{"type": "url_citation", "url_citation": {"url": "https://example.org/c", "title": "C"}}]}}]}
        with patch.object(llm, "_call", return_value=router) as call:
            result = llm.complete("openrouter", "vendor/model", "system", self.messages, meal_agent.SCHEMA, True)
            url, headers, body = call.call_args.args
            self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(body["messages"][1]["content"][1]["type"], "image_url")
            self.assertEqual(body["plugins"][0]["id"], "web")
            self.assertEqual((result["usage"]["cost_usd"], result["sources"][0]["title"]), (0.002, "C"))
            text = llm.transcribe("vendor/audio", "AAAA", "wav", "Transkribiere")
            self.assertEqual(call.call_args.args[2]["messages"][0]["content"][1],
                             {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}})
            self.assertTrue(text["text"])

    def test_missing_key_or_model_is_named(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(llm.configured(), ["anthropic", "openrouter"])
            with self.assertRaisesRegex(llm.LLMError, "OPENAI_API_KEY"):
                llm.complete("openai", "gpt-x", "s", self.messages, meal_agent.SCHEMA)
        with self.assertRaisesRegex(llm.LLMError, "Modell"):
            llm.complete("openrouter", "", "s", self.messages, meal_agent.SCHEMA)


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
        answer = {"data": ANSWER, "sources": [], "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": None}}
        with patch.object(llm, "complete", return_value=answer) as complete:
            first = agent.estimate({"text": "Porridge wie immer", "photos": ["data:image/jpeg;base64," + JPEG],
                                    "at": "2026-11-16T07:30:00+00:00"})
            provider, model, system, messages, schema, web = complete.call_args.args
            self.assertEqual((provider, model, web), ("anthropic", "claude-opus-5-5", True))
            self.assertIn("- Porridge [Frühstück]", messages[0]["text"])
            self.assertIn("Philipp: Porridge wie immer", messages[0]["text"])
            self.assertEqual(len(messages[0]["images"]), 1)
            self.assertEqual(first["estimate"]["dish"]["new"], False)
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
        with patch.object(llm, "complete", side_effect=llm.LLMError("Anbieter lehnt ab (402, kein Guthaben): x")):
            with self.assertRaisesRegex(ValueError, "kein Guthaben"):
                agent.estimate({"text": "Brot"})


if __name__ == "__main__":
    unittest.main()
