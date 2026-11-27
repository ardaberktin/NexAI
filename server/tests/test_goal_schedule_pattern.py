import json
import unittest
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nex.conversation.goal_authorization import requested_future_action
from nex.conversation.goal_edit_fields import edit_schema
from nex.conversation.plan import requested_schedule
from nex.conversation.schedule_pattern import (
    Pattern,
    interval_minutes,
    read_delivery,
    read_pattern,
)
from nex.conversation.verify_tool_reply import mail_claim_matches


class GoalSchedulePatternTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn = SimpleNamespace(started=datetime.fromisoformat("2026-06-01T07:00:00+00:00"), zone="UTC")
        self.context = {"question": "Check for new volunteer shifts twice an hour."}

    async def test_unrounded_period_and_weekday_subsets_never_reach_the_goal_writer(self):
        for minutes, days in ((30, []), (1440, ["Monday", "Wednesday"]), (15, ["Tuesday"])):
            with self.subTest(minutes=minutes, days=days):
                generate = AsyncMock(return_value=json.dumps({"pattern": "interval", "count": minutes, "unit": "minute",
                                      "weekdays": days, "clock": None, "first_start": "implicit"}))
                pattern, plan = await requested_schedule(self.context, generate)
                self.assertIsNone(pattern)
                self.assertEqual((plan.tools, plan.error), ((), "unsupported_goal_schedule"))

    async def test_permission_and_unavailable_classification_are_distinct(self):
        for payload, expected in (({"pattern": "no_request", "count": None, "unit": None, "weekdays": [],
                                   "clock": None, "first_start": "implicit"}, ("", True)),
                                  ({"pattern": "interval", "weekdays": "Friday"}, ("model_unavailable", False))):
            _, plan = await requested_schedule(self.context, AsyncMock(return_value=json.dumps(payload)))
            self.assertEqual((plan.error, plan.clarify, plan.tools), (*expected, ()))

    async def test_cadence_without_a_clock_does_not_invent_a_first_time(self):
        payload = {"pattern": "interval", "count": 1, "unit": "hour", "weekdays": [], "clock": None, "first_start": "clock"}
        result = await read_pattern(self.context, AsyncMock(return_value=json.dumps(payload)))
        self.assertEqual((result.minutes, result.first_start), (60, "implicit"))

    async def test_future_action_needs_authorization_grounded_in_the_latest_message(self):
        message = "Please check on the package tomorrow."
        for decision, expected in (({"intent": "context"}, False),
                                   ({"intent": "request", "words": "check on the package tomorrow"}, True),
                                   ({"intent": "request", "words": "Notify me every hour"}, False)):
            result = await requested_future_action({"question": message}, AsyncMock(return_value=json.dumps(decision)))
            self.assertIs(result, expected)

    def test_interval_conversion_preserves_the_quantity_and_unit(self):
        for count, unit, result in ((15, "minute", 15), (0.5, "hour", 30), (2, "week", 20160),
                                    (True, "minute", None), (1, "unknown", None)):
            with self.subTest(count=count, unit=unit):
                self.assertEqual(interval_minutes({"count": count, "unit": unit}), result)

    async def test_requested_delivery_distinguishes_future_work_from_an_immediate_read(self):
        question = "Each morning, give me a summary of my appointments."
        for when, quote, expected in (("recurring", "Each morning", "recurring"), ("now", None, None),
                                      ("recurring", "each evening", None)):
            decision = {"when": when, "delivery_words": quote}
            self.assertEqual(await read_delivery(question, AsyncMock(return_value=json.dumps(decision))), expected)

    async def test_filter_edit_exposes_only_the_task_field(self):
        data = {"selected_goal": {"title": "Paid shifts"}, "latest_edit_request": "Only weekend openings now."}
        result = await edit_schema(data, AsyncMock(return_value='{"fields":["query"]}'))
        self.assertEqual(result["required"], ["query"])
        self.assertEqual(set(result["properties"]), {"query"})
        self.assertFalse(result["additionalProperties"])

    def test_empty_filtered_search_cannot_establish_an_empty_inbox(self):
        search = {"status": "empty", "messages": [], "search_scope": {"query": "summer placements"}}
        self.assertTrue(mail_claim_matches("checked", search))
        self.assertFalse(mail_claim_matches("inbox_empty", search))
        self.assertTrue(mail_claim_matches("inbox_empty", search | {"search_scope": {}}))

    def test_first_weekly_run_uses_next_actual_occurrence_and_respects_delayed_start(self):
        tool = {"start": "2026-06-15T07:15", "frequency": 1440}
        for hour, first_mode, expected in ((8, "clock", "2026-06-01T08:00"), (6, "clock", "2026-06-08T06:00"),
                                           (8, "date", "2026-06-15T08:00")):
            with self.subTest(hour=hour, mode=first_mode):
                pattern = Pattern("interval", 10080, ("Monday",), time(hour), first_mode)
                self.assertEqual(pattern.bind(tool, self.turn, new_goal=True), {"frequency": 10080, "start": expected})
