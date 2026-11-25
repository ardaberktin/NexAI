"""Exercise account-scoped MCP settings, assistant routing, and scheduled execution."""

import asyncio
import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from nex.assistant import Assistant
from nex.goal_runner import run_prompt
from nex.hosted.auth import insert_user, issue_device
from nex.live.public_reply import public_reply
from nex.mcp import MCPError
from nex.mcp.connections import catalog
from nex.mcp.execution import execute
from nex.mcp.planning import shortlist_goal
from nex.mcp.tool import Tool
from nex.privacy.public import answer
from test_hosted_accounts import HostedAccountFixture, public_key

ROOT = "/v2/workspace/mcp"
NOW = datetime(2026, 11, 25, 17, tzinfo=UTC)
SECRET = "fixture-mcp-token"
READ = {"name": "inventory", "description": "Read available stock for a warehouse",
        "inputSchema": {"type": "object", "properties": {"warehouse": {"type": "string"}}, "required": ["warehouse"]},
        "annotations": {"readOnlyHint": True}}
WRITE = {"name": "place_order", "description": "Create an order", "inputSchema": {"type": "object"}}


class MCPFixture(HostedAccountFixture):
    def setUp(self):
        super().setUp()
        self.store = self.host.owner.store
        self.device = self.register_device("mac")
        self.session = self.unlock(self.device)
        self.client = self.enterContext(patch("nex.mcp.client.connect")).return_value.__enter__.return_value
        self.client.list_tools.return_value = [READ, WRITE]
        self.client.call.return_value = {
            "content": [{"type": "text", "text": "There are 23 monitors in the warehouse."}],
            "structuredContent": {"count": 23, "credential": SECRET},
        }

    def settings(self, method="GET", suffix="", *, body=None, status=200, locked=False, device=None, session=None):
        return self.request(method, ROOT + suffix, (device or self.device)["token"],
                            body=body, workspace=None if locked else session or self.session, status=status)

    def add(self):
        data = {"id": str(uuid4()), "name": "Warehouse", "url": "https://inventory.example/mcp",
                "headers": {"Authorization": "Bearer " + SECRET}}
        result = self.settings("POST", body=data)
        self.assertEqual(self.settings("POST", body=data), result)
        return result

    def permission(self, item, tool, *, status=200, locked=False, **changes):
        return self.settings("PUT", "/" + item["id"] + "/tool-access", body={"tool": tool["name"], **changes},
                             status=status, locked=locked)


class MCPConnectionsTests(MCPFixture):
    def test_management_requires_unlock_and_is_shared_within_one_account(self):
        self.settings(locked=True, status=403)
        self.settings("POST", body={}, locked=True, status=403)
        item = self.add()
        other_device = self.register_device("iphone")
        self.assertEqual(self.settings(device=other_device, session=self.unlock(other_device))["items"], [item])
        self.assertNotIn(SECRET, json.dumps(item))
        self.assertEqual(item["tools"], [
            {"name": "inventory", "read_only": True, "access": "read", "allow_goal_writes": False},
            {"name": "place_order", "read_only": False, "access": "off", "allow_goal_writes": False},
        ])
        with self.accounts.connect(write=True) as db:
            other_id = insert_user(db, "different-account", (bytes(16), bytes(32)), owner=False)
            other = issue_device(db, other_id, "different-account", False, "mac",
                                 base64.b64decode(public_key(self.key)))
        self.assertEqual(self.settings(device=other, session=self.unlock(other))["items"], [])
        suffix = "/" + item["id"]
        self.settings("PUT", suffix, body={"enabled": False})
        self.assertEqual(catalog(self.store), {})
        self.client.list_tools.return_value = [READ]
        self.assertEqual(self.settings("POST", suffix + "/refresh", body={})["tools"], item["tools"][:1])
        self.settings("POST", suffix + "/remove", body={})
        self.assertEqual(self.settings(), {"items": []})

    def test_chat_and_recorded_voice_receive_tool_schemas_and_respect_approval(self):
        self.add()
        alias = next(name for name, (_, tool) in catalog(self.store).items() if tool.name == READ["name"])
        call = {"name": alias, "arguments": {"warehouse": "West"}}
        questions, composed = [], []

        def generate(messages, *_args, **_kwargs):
            data = json.loads(messages[1]["content"])
            if candidates := data.get("mcp_candidates"):
                questions.append(data["latest_message"])
                choice = next(tool for tool in candidates if tool["tool"] == "inventory")
                self.assertEqual(choice["server"], "Warehouse")
                if data["latest_message"].startswith("Explain"):
                    return '{"tools":[],"workflow":"other","language":"en"}'
                return json.dumps({"tools": [choice["number"]], "workflow": "general", "language": "en"})
            if "latest_message" in data:
                return '{"capabilities":["answer"],"language":"en"}'
            if "mcp_tools" in data:
                self.assertEqual(data["mcp_tools"][alias]["input_schema"], READ["inputSchema"])
                return json.dumps({"tools": [call]})
            if "tools" in data:
                composed.append(data["tools"])
                if alias in data["tools"]:
                    self.assertEqual(data["tools"][alias]["structured_content"]["count"], 23)
                    self.assertNotIn(SECRET, messages[1]["content"])
                    return "West has 23 monitors."
            return "Inventory counts are measured per warehouse."

        self.host.owner.assistant = Assistant(self.store, None, None)
        self.host.owner.assistant.generate_conversation = generate
        exposed = "How many screens are at West?"
        identifier = str(uuid4())
        request = {"id": identifier, "text": exposed, "language": "en", "time_zone": "UTC"}
        self.request("POST", "/v2/chat", self.device["token"], body=request, status=403)
        self.client.call.assert_not_called()
        self.request("POST", "/v2/chat", self.device["token"], body=request, workspace=self.session)
        self.client.call.assert_called_once_with("tools/call", {"name": "inventory", "arguments": call["arguments"]},
                                                 argument_headers={})
        self.request("POST", "/v2/chat", self.device["token"], body=request, workspace=self.session)
        self.assertEqual(self.client.call.call_count, 1)

        recorded = "Look up available monitors in the West warehouse."
        result = answer(self.host.owner.assistant, self.store.privacy, str(uuid4()), "mac", recorded, "en", "UTC",
                        spoken=True)
        self.assertEqual(result["engine"], "private_request")
        self.assertEqual(self.client.call.call_count, 1)
        live = "Check the Warehouse connection for the West stock count."

        async def complete(_client, messages, *_tokens, **_options):
            return generate(messages)

        dialogue = SimpleNamespace(memory=self.store, local=None, complete=complete)
        voice = SimpleNamespace(id=str(uuid4()), device="mac", text=live, language="en", zone="UTC", started=NOW,
                                journal=None, coordinates=None, speaker="fixture")
        result = asyncio.run(public_reply(dialogue, voice, (), lambda *_: None))
        self.assertEqual(result["engine"], "private_request")
        self.assertEqual(self.client.call.call_count, 1)
        other = "Explain what inventory means."
        self.request("POST", "/v2/chat", self.device["token"], body=request | {"id": str(uuid4()), "text": other},
                     workspace=self.session)
        self.assertEqual(list(dict.fromkeys(questions)), [exposed, recorded, live, other])
        self.assertEqual(self.client.call.call_count, 1)
        self.assertTrue(composed)

    def test_scheduled_read_only_tools_require_opt_in_and_are_checked_again(self):
        item = self.add()
        self.assertEqual(catalog(self.store, scheduled=True), {})
        self.settings("PUT", "/" + item["id"], body={"allow_goals": True})
        allowed = catalog(self.store, scheduled=True)
        alias, = allowed
        call = {"name": alias, "arguments": {"warehouse": "West"}}
        goal = {"prompt": "Report the West warehouse stock from Warehouse each Friday.", "zone": "UTC", "latest": None}

        def generate(messages, _tokens):
            data = json.loads(messages[1]["content"])
            if "evidence" in data:
                self.assertIn("23 monitors", data["evidence"][alias])
                return "West has 23 monitors."
            self.assertEqual(list(data["mcp_tools"]), [alias])
            self.assertEqual(data["goal"], goal["prompt"])
            return json.dumps({"tools": [call]} if isinstance(data["mcp_tools"][alias], dict) else {"sources": [alias]})

        result = run_prompt(self.store, goal, NOW, generate, None)
        self.assertEqual(result.text, "West has 23 monitors.")
        self.permission(item, WRITE, access="write")
        write_alias = Tool.discovered(WRITE).alias(item["id"])
        with self.assertRaises(MCPError):
            execute(self.store, {"name": write_alias, "arguments": {}}, scheduled=True)
        self.client.list_tools.return_value = [READ | {"annotations": {"readOnlyHint": False}}]
        self.assertIn("changed this tool’s read/write capabilities", run_prompt(self.store, goal, NOW, generate, None).error)
        self.assertEqual(self.client.call.call_count, 1)

    def test_pausing_during_session_setup_blocks_the_tool_call(self):
        item = self.add()
        alias = next(iter(catalog(self.store)))
        self.settings("PUT", "/" + item["id"], body={"allow_goals": True})

        def pause():
            self.settings("PUT", "/" + item["id"], body={"enabled": False})
            return [READ, WRITE]

        self.client.list_tools.side_effect = pause
        with self.assertRaisesRegex(MCPError, "disabled"):
            execute(self.store, {"name": alias, "arguments": {"warehouse": "West"}}, scheduled=True)
        self.client.call.assert_not_called()


    def test_large_catalog_selection_shows_each_tool_to_the_model_without_a_single_oversized_prompt(self):
        tools = {
            f"mcp_{i:016x}": ("Warehouse", Tool(f"inventory_location_{i}", "Read the stock for one branch.",
                                                READ["inputSchema"], True))
            for i in range(90)
        }
        chosen = next(reversed(tools))
        seen = set()

        def generate(messages, _tokens):
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["question"], "Read the stock from the final branch")
            batch = data["mcp_tools"]
            self.assertFalse(seen & batch.keys())
            seen.update(batch)
            return json.dumps({"tools": [chosen] if chosen in batch else []})

        chosen_tools = shortlist_goal(generate, tools, {"goal": "Read the stock from the final branch"})
        self.assertEqual(seen, tools.keys())
        self.assertEqual(chosen_tools, {chosen: tools[chosen]})
