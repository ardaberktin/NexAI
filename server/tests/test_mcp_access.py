"""Exercise MCP grants through Settings, Chat and private voice approvals."""

import json
from uuid import uuid4

from nex.assistant import Assistant
from nex.mcp import MCPError
from nex.mcp.connections import catalog
from nex.mcp.execution import execute
from nex.mcp.tool import Tool
from nex.privacy.public import answer
from nex.store import Store
from test_mcp_connections import READ, WRITE, MCPFixture

MIXED = {"name": "order", "description": "Read or update an order using the operation argument.",
         "inputSchema": {"type": "object", "required": ["operation", "id"]}}


class MCPAccessTests(MCPFixture):
    def test_permissions_apply_to_discovered_tools_individually_and_survive_refresh(self):
        item = self.add()
        read, write = (Tool.discovered(tool).alias(item["id"]) for tool in (READ, WRITE))
        self.assertEqual(set(catalog(self.store)), {read})
        self.permission(item, WRITE, access="write", locked=True, status=403)
        self.permission(item, WRITE, access="invalid", status=400)
        self.permission(item, WRITE, allow_goal_writes=True, status=400)
        self.permission(item, READ, access="write", allow_goal_writes=True, status=400)
        self.permission(item, READ, access="write")
        self.permission(item, WRITE, access="read")
        self.assertEqual(catalog(self.store), {})

        self.permission(item, WRITE, access="read_write", allow_goal_writes=True)
        self.permission(item, READ, access="off")
        self.assertEqual(set(catalog(self.store, scheduled=True)), {write})
        self.assertEqual(self.settings("POST", "/" + item["id"] + "/refresh", body={})["tools"], [
            {"name": "inventory", "read_only": True, "access": "off", "allow_goal_writes": False},
            {"name": "place_order", "read_only": False, "access": "read_write", "allow_goal_writes": True},
        ])
        self.permission(item, WRITE, access="read")
        result = self.permission(item, WRITE, access="write")
        self.assertFalse(result["tools"][1]["allow_goal_writes"])
        self.assertEqual(set(catalog(self.store)), {write})
        self.assertEqual(catalog(self.store, scheduled=True), {})
        self.client.list_tools.return_value = [READ]
        self.settings("POST", "/" + item["id"] + "/refresh", body={})
        self.client.list_tools.return_value = [READ, WRITE]
        result = self.settings("POST", "/" + item["id"] + "/refresh", body={})
        self.assertEqual(result["tools"][1]["access"], "off")

    def test_upgrading_a_saved_connection_preserves_reads_and_requires_write_opt_in(self):
        item = self.add()
        with self.store.connect() as db:
            db.execute("DROP TABLE mcp_tool_permissions")
            db.execute("DROP TABLE mcp_goal_runs")
        reopened = Store(self.store.root, 0, 2**20)
        self.assertEqual(self.settings()["items"], [item])
        self.assertEqual(set(catalog(reopened)), {Tool.discovered(READ).alias(item["id"])})

    def test_live_metadata_cannot_turn_a_granted_read_into_a_write(self):
        item = self.add()
        read = Tool.discovered(READ).alias(item["id"])
        self.permission(item, READ, access="read_write")
        self.client.list_tools.return_value = [READ | {"annotations": {"readOnlyHint": False}}]
        with self.assertRaisesRegex(MCPError, "changed this tool’s read/write capabilities"):
            execute(self.store, {"name": read, "arguments": {"warehouse": "West"}})
        self.client.call.assert_not_called()

    def test_chat_writes_and_mixed_tool_reads_follow_the_selected_access(self):
        self.client.list_tools.return_value = [MIXED]
        item = self.add()
        alias = Tool.discovered(MIXED).alias(item["id"])
        mutations = "Change order 45 to quantity 2."
        lookup = "Check order 45."
        overrides = {}
        seen = {}

        def generate(messages, *_args, **_kwargs):
            data = json.loads(messages[1]["content"])
            if "mcp_candidates" in data:
                return '{"tools":[1],"workflow":"general","language":"en"}'
            if "mcp_tools" in data:
                change = data["question"] == mutations
                args = {"operation": "update" if change else "get", "id": 45}
                if change:
                    args["quantity"] = 2
                return json.dumps({"tools": [{"name": alias, "arguments": args}]})
            if "latest_request" in data:
                self.assertEqual(data["arguments"]["id"], 45)
                return overrides.get(data["latest_request"], "WRITE" if data["latest_request"] == mutations else "READ")
            if "tools" in data:
                seen[data["question"]] = data["tools"][alias]
            return "The provider result was processed."

        self.host.owner.assistant = Assistant(self.store, None, None)
        self.host.owner.assistant.generate_conversation = generate

        def chat(text, identifier=None):
            return self.request("POST", "/v2/chat", self.device["token"],
                                body={"id": identifier or str(uuid4()), "text": text, "language": "en", "time_zone": "UTC"},
                                workspace=self.session)

        self.permission(item, MIXED, access="write")
        chat(lookup)
        self.assertEqual(seen[lookup]["status"], "denied")
        self.client.call.assert_not_called()
        identifier = str(uuid4())
        chat(mutations, identifier)
        self.assertEqual(seen[mutations]["operation"], "write")
        self.client.call.assert_called_once_with("tools/call", {"name": "order", "arguments": {
            "operation": "update", "id": 45, "quantity": 2,
        }}, argument_headers={})
        chat(mutations, identifier)
        self.assertEqual(self.client.call.call_count, 1)

        self.permission(item, MIXED, access="read_write")
        chat(lookup)
        self.assertEqual(seen[lookup]["operation"], "read")
        self.assertEqual(self.client.call.call_count, 2)
        overrides[mutations] = "DENY"
        chat(mutations)
        self.assertEqual(seen[mutations]["status"], "denied")
        self.assertEqual(self.client.call.call_count, 2)

    def test_revoking_write_access_after_voice_planning_blocks_resolution(self):
        item = self.add()
        self.permission(item, WRITE, access="write")
        alias = Tool.discovered(WRITE).alias(item["id"])
        identifier = str(uuid4())
        seen = {}

        def generate(messages, *_args, **_kwargs):
            data = json.loads(messages[1]["content"])
            if "mcp_candidates" in data:
                index = next(tool["number"] for tool in data["mcp_candidates"] if tool["tool"] == WRITE["name"])
                return json.dumps({"tools": [index], "workflow": "general", "language": "en"})
            if "mcp_tools" in data:
                return json.dumps({"tools": [{"name": alias, "arguments": {}}]})
            if "tools" in data:
                seen.update(data["tools"][alias])
            return "The provider did not confirm an order."

        self.host.owner.assistant = Assistant(self.store, None, None)
        self.host.owner.assistant.generate_conversation = generate
        outcome = answer(self.host.owner.assistant, self.store.privacy, identifier, "mac", "Place an order.", "en", "UTC",
                         spoken=True)
        self.assertEqual(outcome["engine"], "private_request")
        self.permission(item, WRITE, access="off")
        self.request("POST", "/v2/privacy/resolve", self.device["token"], body={"id": identifier}, workspace=self.session)
        self.assertEqual(seen["status"], "unavailable")
        self.client.call.assert_not_called()
