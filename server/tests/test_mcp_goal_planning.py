"""Verify MCP goal scheduling keeps a separately selected cadence and source."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from nex.conversation.goal_plans import MCP_CADENCE
from nex.conversation.plan import Turn, choose
from nex.mcp.tool import Tool


class ConnectedGoalPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_routing_label_cannot_turn_a_weekly_connected_goal_into_one_run(self):
        tool = Tool("inventory", "Look up inventory", {"type": "object"}, True)
        now = datetime.now(UTC)
        friday = now + timedelta(days=(4 - now.weekday()) % 7 or 7)
        due = friday.strftime("%Y-%m-%d") + "T09:00"
        latest = "Every Friday at nine, report on inventory using Warehouse."
        turn = Turn(str(uuid4()), "mac", latest, "en", "UTC", started=now,
                    mcp_tools={tool.alias(str(uuid4())): ("Warehouse", tool)})
        proposals = []

        async def generate(messages, _tokens):
            data = json.loads(messages[1]["content"])
            if "mcp_candidates" in data:
                return '{"workflow":"schedule","tools":[1],"language":"en"}'
            if messages[0]["content"] == MCP_CADENCE:
                self.assertEqual(data["question"], latest)
                return '{"weekly_day":"Friday","cadence":"daily"}'
            if "proposal" not in data or data["proposal"] is None:
                self.assertEqual(data["mcp_sources"], ["Warehouse"])
                self.assertEqual(data["mcp_frequency"], 10080)
                args = {"name": "goal", "query": "Report inventory using Warehouse", "start": due}
                if proposals:
                    args["frequency"] = 10080
                proposals.append(args)
                return json.dumps({"tools": [args]})
            return "YES"

        plan = await choose(turn, generate)
        self.assertEqual(plan.tools, (proposals[-1],))
        self.assertEqual(plan.tools[0]["frequency"], 10080)
        self.assertEqual(len(proposals), 2)

    async def test_dropping_the_selected_connection_blocks_the_goal(self):
        tool = Tool("inventory", "Look up inventory", {"type": "object"}, True)
        turn = Turn(str(uuid4()), "mac", "Use Warehouse to report available inventory every day.", "en", "UTC",
                    mcp_tools={tool.alias(str(uuid4())): ("Warehouse", tool)})
        due = (turn.started + timedelta(days=1)).strftime("%Y-%m-%d") + "T09:00"

        async def generate(messages, _tokens):
            data = json.loads(messages[1]["content"])
            if "mcp_candidates" in data:
                return '{"workflow":"recurring","tools":[1],"language":"en"}'
            if messages[0]["content"] == MCP_CADENCE:
                return '{"weekly_day":null,"cadence":"daily"}'
            self.assertEqual(data["mcp_sources"], ["Warehouse"])
            return json.dumps({"tools": [{"name": "goal", "query": "Report available inventory",
                                         "start": due, "frequency": 1440}]})

        plan = await choose(turn, generate)
        self.assertEqual(plan.tools, ())
        self.assertEqual(plan.error, "invalid_goal_plan")
