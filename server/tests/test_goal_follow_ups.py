import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

from nex.feed import stamp
from nex.follow_up_timing import CHOOSE_TIMING
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goal_follow_ups import answer
from nex.goals import Goals
from nex.store import Store
from nex.workspace_chat_pages import chat_page
from nex.workspace_content import chat_history
from nex.workspace_schema import WorkspaceConflict


class GoalFollowUpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name, 0, 2**20)
        self.generate = Mock()
        self.timing = Mock(return_value='{"start":"2026-12-01T13:00"}')
        self.goals = Goals(self.store)
        self.jobs = GoalFollowUpJobs(self.store, self.generate_tool)
        self.id = str(uuid4())
        self.now = datetime(2026, 12, 1, 16, tzinfo=UTC)
        self.draft = {"prompt": "Check whether the mechanic finished my repair.", "zone": "America/Vancouver"}

    def generate_tool(self, messages, tokens):
        return (self.timing if messages[0]["content"].startswith(CHOOSE_TIMING) else self.generate)(messages, tokens)

    def tick(self, anchor):
        self.jobs.deliver(anchor)
        self.jobs.decide(anchor)

    def detail(self):
        return self.goals.detail(self.id)

    def utc(self, local):
        return datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(self.draft["zone"])).astimezone(UTC)

    def test_nex_chooses_the_time_and_text_again_after_the_users_answer(self):
        first = self.utc("2026-12-01T13:00")
        second = self.utc("2026-12-03T15:15")
        self.goals.save(self.id, self.draft, self.now, request="I'm waiting for the mechanic to finish.", language="en")
        initial_call = {"schedule_follow_up": {
            "question": "Is the car ready to pick up?", "choices": ["Still in the shop", "It's ready"],
        }}
        self.generate.side_effect = [
            json.dumps(initial_call),
            self.schedule("Did the shop finish the repair?", "They finished it", "Still waiting"),
            '{"tool_call":{"name":"complete_goal"}}',
        ]
        self.timing.side_effect = [
            json.dumps({"start": first.astimezone(ZoneInfo(self.draft["zone"])).isoformat(),
                        "days_from_now": 0, "clock": "13:00"}),
            '{"start":"2026-12-03T15:40","days_from_now":2,"clock":"15:15"}',
            '{"start":"2026-12-03T15:15"}',
        ]
        with self.store.connect() as db:
            db.execute("INSERT INTO iphone_push_registration VALUES(1,'test','test','development')")
        self.tick(self.now)
        upcoming = self.detail()["follow_up"]
        self.assertEqual(upcoming["due"], stamp(first))
        self.assertIsNone(upcoming["question"])
        self.tick(first - timedelta(seconds=1))
        self.assertEqual(chat_history(self.store), [])
        self.tick(first)
        item, = chat_page(self.store)["items"]
        follow_up = self.detail()["follow_up"]
        self.assertEqual(item["follow_up"], follow_up)
        self.assertEqual(item["text"], "Is the car ready to pick up?")
        self.assertEqual(follow_up["choices"], ["Still in the shop", "It's ready"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT id FROM goal_push_delivery").fetchone()[0], item["id"])
        receipt = answer(self.store, self.id, follow_up["id"], 0, first)
        self.assertEqual(answer(self.store, self.id, follow_up["id"], 1, first), receipt)
        self.assertEqual(chat_history(self.store)[0]["follow_up"]["selected_choice"], 0)
        self.tick(first)
        upcoming = self.detail()["follow_up"]
        self.assertEqual(upcoming["due"], stamp(second))
        data = json.loads(self.generate.call_args.args[0][1]["content"])
        self.assertEqual(data["latest_answer"]["selected"], "Still in the shop")
        self.assertEqual(data["original_request"], "I'm waiting for the mechanic to finish.")
        self.tick(second)
        follow_up = self.detail()["follow_up"]
        self.assertEqual(follow_up["question"], "Did the shop finish the repair?")
        self.assertEqual(follow_up["choices"], ["They finished it", "Still waiting"])
        answer(self.store, self.id, follow_up["id"], 0, second)
        self.tick(second)
        data = json.loads(self.generate.call_args.args[0][1]["content"])
        self.assertEqual(data["latest_answer"]["selected"], "They finished it")
        self.assertEqual(data["earlier_answers"][0]["selected"], "Still in the shop")
        self.assertEqual(self.detail()["status"], "completed")
        self.assertEqual(len(chat_history(self.store)), 2)

    def test_nex_can_reuse_an_existing_check_in_run_without_a_duplicate_post(self):
        due = self.now + timedelta(days=1)
        self.goals.save(self.id, self.draft | {"scheduled_at": stamp(due)}, self.now)
        self.generate.return_value = json.dumps({
            "name": "schedule_follow_up", "use_goal_run": True, "question": "Is the repair finished?",
            "choices": ["It is", "Still waiting"],
        })
        self.tick(self.now)
        ordinary = Mock()
        goals = Goals(self.store, generate=ordinary)
        goals.run(due)
        self.tick(due)
        goals.run(due)
        ordinary.assert_not_called()
        self.assertIsNone(self.detail()["next_run"])
        self.assertEqual(len(chat_history(self.store)), 1)

    def test_invalid_decision_retries_and_nex_can_skip_a_follow_up(self):
        due = self.now + timedelta(hours=1)
        self.goals.save(self.id, self.draft | {
            "prompt": "Check Gmail for messages about the repair.", "scheduled_at": stamp(due), "frequency_minutes": 60,
        }, self.now)
        self.generate.side_effect = [
            self.schedule("Has the repair finished?", "Yes", "No"),
            '{"name":"complete_goal"}',
            '{"name":"leave_as_is"}',
        ]
        self.timing.return_value = '{"start":"2026-12-01T07:45"}'
        self.tick(self.now)
        self.tick(self.now + timedelta(seconds=30))
        self.assertEqual(self.generate.call_count, 1)
        self.tick(self.now + timedelta(minutes=1))
        self.assertIsNone(self.detail()["follow_up"])
        self.assertEqual((self.detail()["status"], self.detail()["next_run"]), ("active", stamp(due)))

    def test_edit_during_generation_discards_stale_assessment(self):
        original = self.goals.save(self.id, self.draft, self.now)

        def edit(*_):
            self.goals.save(self.id, self.draft | {
                "prompt": "Watch Gmail for updates instead.", "revision": original["revision"],
            }, self.now)
            return self.schedule("Is the car ready?", "Ready", "Still waiting")

        self.generate.side_effect = edit
        self.tick(self.now)
        self.assertIsNone(self.detail()["follow_up"])
        self.generate.side_effect = None
        self.generate.return_value = '{"name":"leave_as_is"}'
        self.tick(self.now)
        self.assertIn("Watch Gmail", self.detail()["prompt"])

    def test_goal_status_controls_delivery_and_answering(self):
        goal = self.goals.save(self.id, self.draft, self.now)
        due = self.utc("2026-12-01T13:00")
        self.generate.return_value = self.schedule("Is the car ready?", "Ready", "Still waiting")
        self.tick(self.now)
        identifier = self.detail()["follow_up"]["id"]
        with self.assertRaises(WorkspaceConflict):
            answer(self.store, self.id, identifier, 0, self.now)
        paused = self.goals.action(self.id, "paused", goal["revision"], self.now)
        self.tick(due)
        self.assertEqual(chat_history(self.store), [])
        active = self.goals.action(self.id, "active", paused["revision"], due)
        self.tick(due)
        self.goals.action(self.id, "completed", active["revision"], due)
        with self.assertRaises(WorkspaceConflict):
            answer(self.store, self.id, identifier, 0, due)
        self.assertFalse(chat_page(self.store)["items"][0]["follow_up"]["can_answer"])

    @staticmethod
    def schedule(question, *choices):
        return json.dumps({"name": "schedule_follow_up", "question": question, "choices": choices})
