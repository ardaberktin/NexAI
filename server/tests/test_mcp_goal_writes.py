"""Exercise scheduled MCP write authorization and durable dispatch receipts."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

from nex.goals import Goals
from nex.mcp import MCPError
from nex.mcp.connections import catalog
from nex.mcp.tool import Tool
from test_mcp_connections import NOW, WRITE, MCPFixture


class MCPGoalWriteTests(MCPFixture):
    def setUp(self):
        super().setUp()
        self.item = self.add()
        self.permission(self.item, WRITE, access="write")
        self.alias = Tool.discovered(WRITE).alias(self.item["id"])
        self.id = str(uuid4())
        self.prompt = "Place an order for three monitors in Warehouse."
        self.goals = Goals(self.store, generate=self.generate)
        self.goals.save(self.id, {"prompt": self.prompt, "zone": "UTC", "scheduled_at": NOW.isoformat(),
                                  "frequency_minutes": 60}, NOW - timedelta(minutes=1))
        self.verdict = "WRITE"
        self.interrupt = False
        self.latest_evidence = None
        self.source_plans = 0

    def generate(self, messages, _tokens):
        data = json.loads(messages[1]["content"])
        if "latest_request" in data:
            self.assertEqual(data["latest_request"], self.prompt)
            self.assertFalse(data["allow_read"])
            return self.verdict
        if "evidence" in data:
            self.latest_evidence = data["evidence"]
            if self.interrupt:
                raise RuntimeError("Interrupted after external completion")
            return "Warehouse returned the order receipt."
        self.assertEqual(data["goal"], self.prompt)
        if "mcp_tools" not in data or self.alias not in data["mcp_tools"]:
            return '{"sources":["none"]}'
        if isinstance(data["mcp_tools"][self.alias], dict):
            return json.dumps({"tools": [{"name": self.alias, "arguments": {"quantity": 3}}]})
        self.source_plans += 1
        return json.dumps({"sources": [self.alias]})

    def enable(self):
        self.permission(self.item, WRITE, allow_goal_writes=True)

    def test_goal_permission_is_separate_and_prompt_must_authorize_the_write(self):
        self.assertNotIn(self.alias, catalog(self.store, scheduled=True))
        self.enable()
        self.verdict = "DENY"
        self.goals.run(NOW)
        self.client.call.assert_not_called()
        self.assertIn("could not authorize", self.goals.detail(self.id)["error"])

    def test_completed_write_is_recovered_from_receipt_when_composing_is_interrupted(self):
        self.enable()
        self.client.call.return_value = {"content": [{"type": "text", "text": "Order 45 created"}]}
        self.interrupt = True
        with self.assertRaisesRegex(RuntimeError, "Interrupted"):
            self.goals.run(NOW)
        self.client.call.assert_called_once()
        self.interrupt = False
        self.goals.run(NOW + timedelta(minutes=1))
        self.assertIn("Order 45 created", self.latest_evidence[self.alias])
        self.assertEqual(self.source_plans, 1)
        self.client.call.assert_called_once()
        self.assertIn("MCP write call completed", [row["title"] for row in self.goals.detail(self.id)["activity"]])

        self.goals.run(NOW + timedelta(hours=1))
        self.assertEqual(self.client.call.call_count, 2)

    def test_concurrent_worker_leaves_an_inflight_write_to_its_owner(self):
        self.enable()
        entered, release = Event(), Event()

        def slow_write(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"content": [{"type": "text", "text": "Order 45 created"}]}

        self.client.call.side_effect = slow_write
        with ThreadPoolExecutor(max_workers=1) as worker:
            active = worker.submit(self.goals.run, NOW)
            try:
                self.assertTrue(entered.wait(3))
                Goals(self.store, generate=self.generate).run(NOW)
                self.assertIsNone(self.goals.detail(self.id)["latest"])
                self.client.call.assert_called_once()
            finally:
                release.set()
            active.result()
        self.assertIsNone(self.goals.detail(self.id)["error"])
        self.assertIn("Order 45 created", self.latest_evidence[self.alias])

    def test_unconfirmed_dispatch_waits_for_next_scheduled_occurrence(self):
        self.enable()
        self.client.call.side_effect = MCPError("The transport closed before reporting a result.")
        self.goals.run(NOW)
        detail = self.goals.detail(self.id)
        self.assertIn("could not confirm", detail["error"])
        self.assertIn("MCP tool call needs review", [row["title"] for row in detail["activity"]])
        self.assertIn("unconfirmed", self.latest_evidence[self.alias])
        self.goals.run(NOW + timedelta(minutes=5))
        self.client.call.assert_called_once()
        self.assertEqual(self.source_plans, 1)
