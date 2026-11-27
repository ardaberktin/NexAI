import json
import unittest
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nex.conversation.plan import requested_schedule
from nex.conversation.schedule_pattern import Pattern, read_pattern


class GoalSchedulePatternTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn = SimpleNamespace(started=datetime.fromisoformat("2026-06-01T07:00:00+00:00"), zone="UTC")
        self.context = {"question": "Check for new volunteer shifts twice an hour."}

    async def test_unrounded_period_and_weekday_subsets_never_reach_the_goal_writer(self):
        for minutes, days in ((30, []), (1440, ["Monday", "Wednesday"]), (15, ["Tuesday"])):
            with self.subTest(minutes=minutes, days=days):
                generate = AsyncMock(return_value=json.dumps({"pattern": "interval", "minutes": minutes,
                                      "weekdays": days, "clock": None, "first_start": "implicit"}))
                pattern, plan = await requested_schedule(self.context, generate)
                self.assertIsNone(pattern)
                self.assertEqual((plan.tools, plan.error), ((), "unsupported_goal_schedule"))

    async def test_permission_and_unavailable_classification_are_distinct(self):
        for payload, expected in (({"pattern": "no_request", "minutes": None, "weekdays": [],
                                   "clock": None, "first_start": "implicit"}, ("", True)),
                                  ({"pattern": "interval", "weekdays": "Friday"}, ("model_unavailable", False))):
            _, plan = await requested_schedule(self.context, AsyncMock(return_value=json.dumps(payload)))
            self.assertEqual((plan.error, plan.clarify, plan.tools), (*expected, ()))

    async def test_cadence_without_a_clock_does_not_invent_a_first_time(self):
        payload = {"pattern": "interval", "minutes": 60, "weekdays": [], "clock": None, "first_start": "clock"}
        result = await read_pattern(self.context, AsyncMock(return_value=json.dumps(payload)))
        self.assertEqual((result.minutes, result.first_start), (60, "implicit"))

    def test_first_weekly_run_uses_next_actual_occurrence_and_respects_delayed_start(self):
        tool = {"start": "2026-06-15T07:15", "frequency": 1440}
        for hour, first_mode, expected in ((8, "clock", "2026-06-01T08:00"), (6, "clock", "2026-06-08T06:00"),
                                           (8, "date", "2026-06-15T08:00")):
            with self.subTest(hour=hour, mode=first_mode):
                pattern = Pattern("interval", 10080, ("Monday",), time(hour), first_mode)
                self.assertEqual(pattern.bind(tool, self.turn, new_goal=True), {"frequency": 10080, "start": expected})
