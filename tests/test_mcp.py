import base64
import hashlib
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tests import test_integration
from tests.test_integration import FakeNightscout

KEY = "m" * 40


class McpTests(unittest.TestCase):
    setUp = test_integration.IntegrationTests.setUp
    tearDown = test_integration.IntegrationTests.tearDown
    request = test_integration.IntegrationTests.request

    def mcp(self, message, token=KEY, headers=None):
        raw = json.dumps(message).encode()
        all_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        req = urllib.request.Request(self.base + "/mcp", data=raw, headers=all_headers, method="POST")
        with urllib.request.urlopen(req) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None

    def status(self, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.mcp(*args, **kwargs)
        error.exception.close()
        return error.exception.code

    def call(self, name, **arguments):
        _, answer = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
        result = answer["result"]
        if result["isError"]:
            raise ValueError(result["content"][0]["text"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def fresh_glucose(self, value=150):
        now = datetime.now(timezone.utc)
        FakeNightscout.entries = [
            {"_id": f"g{minutes}", "date": int((now - timedelta(minutes=minutes)).timestamp() * 1000),
             "type": "sgv", "sgv": value} for minutes in (2, 7, 12)]
        self.request("/api/settings")  # Sync the current Nightscout I:C and KF schedules.

    def test_only_the_mcp_key_opens_the_endpoint(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.status(message), 401)
        os.environ["MCP_API_KEY"] = "short"
        self.assertEqual(self.status(message, token="short"), 401)
        os.environ["MCP_API_KEY"] = KEY
        os.environ["API_READ_KEY"] = "r" * 40
        self.assertEqual(self.status(message, token="r" * 40), 401)
        self.assertEqual(self.status(message, token=None), 401)
        self.assertEqual(self.status(message, headers={"Origin": "https://evil.example"}), 403)
        self.assertEqual(self.mcp(message), (200, {"jsonrpc": "2.0", "id": 1, "result": {}}))
        # The MCP key opens nothing else.
        req = urllib.request.Request(self.base + "/api/settings", headers={"Authorization": f"Bearer {KEY}"})
        os.environ["APP_PASSWORD"] = "secret"
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(self.base + "/mcp"))
        self.assertEqual(error.exception.code, 405)
        error.exception.close()

    def test_handshake_and_tool_list(self):
        os.environ["MCP_API_KEY"] = KEY
        _, answer = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(answer["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", answer["result"]["capabilities"])
        self.assertIn("preview_entry", answer["result"]["instructions"])
        self.assertEqual(self.mcp({"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))
        _, tools = self.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in tools["result"]["tools"]]
        self.assertEqual(names, ["get_status", "preview_entry", "save_entry", "save_basal", "delete_entry",
                                 "get_day", "read_data"])
        save = next(tool for tool in tools["result"]["tools"] if tool["name"] == "save_entry")
        self.assertEqual(save["inputSchema"]["properties"]["carbs_certainty"]["enum"], ["weighed", "known", "unsure"])
        _, unknown = self.mcp({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_meal_is_previewed_and_saved_with_certainty_and_fat_mark_once(self):
        os.environ["MCP_API_KEY"] = KEY
        self.fresh_glucose()
        preview = self.call("preview_entry", carbs_g=45)
        options = {option["variant"]: option for option in preview["dose"]["options"]}
        self.assertEqual(sorted(options), ["carbs", "cautious", "normal"])
        self.assertGreater(Decimal(options["normal"]["units"]), Decimal(options["cautious"]["units"]))
        self.assertIsNone(preview["split"])
        self.assertIn("units_per_ke", preview["dose"]["factor"])
        self.assertEqual(FakeNightscout.treatments, [])
        event_id = "4a0e7a1e-8b0f-4c55-9c62-3f2f0c0e5d11"
        saved = self.call("save_entry", carbs_g=45, insulin_units=5, carbs_certainty="known", fat_protein=True,
                          dose_variant="normal", notes="Pasta", event_id=event_id)
        self.assertEqual((saved["kind"], saved["carbs_g"], saved["insulin_units"]), ("meal", "45", "5"))
        self.assertEqual((saved["entry"]["carbs_certainty"], saved["entry"]["fat_protein"]), ("known", True))
        self.call("save_entry", carbs_g=45, insulin_units=5, carbs_certainty="known", fat_protein=True,
                  event_id=event_id)
        self.assertEqual(len(FakeNightscout.treatments), 1)
        record = FakeNightscout.treatments[0]
        self.assertEqual((record["carbs"], record["insulin"]), (45, 5))
        self.assertIn("[DAQ:known]", record["notes"])
        self.assertIn("[DAQ:fat]", record["notes"])
        self.assertTrue(record["notes"].startswith("Pasta"))
        # A snack right after it is assigned to that meal.
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        snack = self.call("preview_entry", carbs_g=10, at=later)
        self.assertTrue(snack["assignment"]["snack"])
        status = self.call("get_status")
        self.assertEqual(Decimal(status["glucose"]["glucose_mgdl"]), Decimal(150))
        self.assertEqual([row["carbs"] for row in status["entries_today"]], [45])
        self.assertGreater(Decimal(status["on_board"]["insulin_units"]), 0)

    def test_split_and_correction_and_delete(self):
        os.environ["MCP_API_KEY"] = KEY
        self.fresh_glucose()
        preview = self.call("preview_entry", carbs_g=100)
        self.assertTrue(preview["split"]["active"])
        normal = next(option for option in preview["dose"]["options"] if option["variant"] == "normal")
        without = self.call("preview_entry", carbs_g=100, split=False)
        self.assertFalse(without["split"]["active"])
        self.assertNotIn("later_units", without["dose"]["options"][0])
        self.assertEqual(Decimal(normal["units"]) + Decimal(normal["later_units"]), Decimal(normal["full_units"]))
        saved = self.call("save_entry", carbs_g=100, insulin_units=normal["units"], carbs_certainty="weighed",
                          fat_protein=False, dose_variant="normal")
        self.assertEqual(Decimal(saved["split_second_part_units"]), Decimal(normal["later_units"]))
        self.assertIn("[DASP:", FakeNightscout.treatments[0]["notes"])
        correction = self.call("save_entry", insulin_units=1.5)
        self.assertEqual(correction["kind"], "correction")
        self.assertEqual(FakeNightscout.treatments[-1]["eventType"], "Correction Bolus")
        day = self.call("get_day")
        self.assertEqual([row["kind"] for row in day["entries"]], ["meal", "correction"])
        self.call("delete_entry", id=correction["id"])
        self.assertEqual(len(FakeNightscout.treatments), 1)

    def test_basal_sport_and_wrong_arguments(self):
        os.environ["MCP_API_KEY"] = KEY
        self.fresh_glucose()
        basal = self.call("save_basal", units=17)
        self.assertTrue(basal["nightscout_synced"])
        sport = self.call("save_entry", kind="exercise", sport_kind="jogging", duration_minutes=30)
        self.assertEqual(sport["kind"], "exercise")
        with self.assertRaisesRegex(ValueError, "carbs_certainty"):
            self.call("save_entry", carbs_g=20, insulin_units=2, carbs_certainty="ganz sicher")
        with self.assertRaisesRegex(ValueError, "Kohlenhydrate, Insulin"):
            self.call("save_entry")
        with self.assertRaisesRegex(ValueError, "Basal"):
            self.call("save_basal", units=17.3)
        _, answer = self.mcp({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope"}})
        self.assertEqual(answer["error"]["code"], -32602)
        meals = self.call("read_data", table="meals", days=3)
        self.assertEqual(meals["rows"], [])

    def test_claude_app_connects_through_oauth_with_the_app_password(self):
        os.environ.update({"MCP_API_KEY": KEY, "APP_PASSWORD": "secret"})
        callback = "https://claude.ai/api/mcp/auth_callback"
        no_redirect = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {
            "redirect_request": lambda *args: None}))

        def post(path, data, json_body=False):
            raw = json.dumps(data).encode() if json_body else urllib.parse.urlencode(data).encode()
            kind = "application/json" if json_body else "application/x-www-form-urlencoded"
            req = urllib.request.Request(self.base + path, data=raw, headers={"Content-Type": kind}, method="POST")
            try:
                with no_redirect.open(req) as response:
                    return response.status, response.headers, response.read().decode()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.headers, error.read().decode()

        # The 401 points the client to the metadata, which names the endpoints.
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="nope")
        self.assertIn("/.well-known/oauth-protected-resource", denied.exception.headers["WWW-Authenticate"])
        denied.exception.close()
        with urllib.request.urlopen(self.base + "/.well-known/oauth-authorization-server") as response:
            metadata = json.loads(response.read())
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        status, _, body = post("/oauth/register", {"redirect_uris": ["https://evil.example/cb"]}, json_body=True)
        self.assertEqual(status, 400)
        status, _, body = post("/oauth/register", {"redirect_uris": [callback], "client_name": "Claude"}, json_body=True)
        self.assertEqual(status, 201)
        client_id = json.loads(body)["client_id"]
        verifier = "v" * 50
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        request = {"response_type": "code", "client_id": client_id, "redirect_uri": callback,
                   "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz"}
        with urllib.request.urlopen(self.base + "/oauth/authorize?" + urllib.parse.urlencode(request)) as response:
            self.assertIn("claude.ai", response.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as evil:
            urllib.request.urlopen(self.base + "/oauth/authorize?" + urllib.parse.urlencode(
                {**request, "redirect_uri": "https://evil.example/cb"}))
        self.assertEqual(evil.exception.code, 400)
        evil.exception.close()
        status, _, body = post("/oauth/authorize", {**request, "password": "wrong"})
        self.assertEqual(status, 200)
        self.assertIn("Passwort stimmt nicht", body)
        status, headers, _ = post("/oauth/authorize", {**request, "password": "secret"})
        self.assertEqual(status, 303)
        location = urllib.parse.urlparse(headers["Location"])
        self.assertEqual(location._replace(query="").geturl(), callback)
        query = urllib.parse.parse_qs(location.query)
        self.assertEqual(query["state"], ["xyz"])
        exchange = {"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": callback,
                    "client_id": client_id, "code_verifier": "x" * 50}
        self.assertEqual(post("/oauth/token", exchange)[0], 400)  # Wrong verifier, and the code is spent.
        status, headers, _ = post("/oauth/authorize", {**request, "password": "secret"})
        exchange["code"] = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)["code"][0]
        status, _, body = post("/oauth/token", {**exchange, "code_verifier": verifier})
        self.assertEqual(status, 200)
        tokens = json.loads(body)
        self.assertEqual(post("/oauth/token", {**exchange, "code_verifier": verifier})[0], 400)
        self.assertEqual(self.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=tokens["access_token"])[0], 200)
        self.assertEqual(self.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=tokens["refresh_token"]), 401)
        status, _, body = post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        self.assertEqual(status, 200)
        renewed = json.loads(body)["access_token"]
        self.assertEqual(self.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=renewed)[0], 200)
        # A new MCP_API_KEY ends every connection.
        os.environ["MCP_API_KEY"] = "n" * 40
        self.assertEqual(self.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=renewed), 401)


if __name__ == "__main__":
    unittest.main()
