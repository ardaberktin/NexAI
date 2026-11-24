import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

from nex.follow_up_timing import CHOOSE_TIMING
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goal_follow_up_notices import GoalFollowUpNotices
from nex.goal_follow_ups import answer
from nex.goals import Goals
from nex.store import Store
from nex.workspace_chat_pages import paged_chat_snapshot
from nex.workspace_content import artifacts, chat_history


class GoalFollowUpNoticeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name, 0, 2**20)
        self.goals = Goals(self.store)
        self.generate = Mock()
        self.notices = GoalFollowUpNotices(self.store, self.generate)
        self.plan = Mock()
        self.timing = Mock()
        self.jobs = GoalFollowUpJobs(self.store, self.generate_tool)
        self.id = str(uuid4())
        self.now = datetime(2026, 7, 1, 20, tzinfo=UTC)
        self.draft = {"prompt": "Ask whether my replacement card arrived.", "zone": "America/Vancouver"}

    def generate_tool(self, messages, tokens):
        return (self.timing if messages[0]["content"].startswith(CHOOSE_TIMING) else self.plan)(messages, tokens)

    def schedule(self, timing, question, *, anchor=None):
        self.plan.return_value = json.dumps({
            "name": "schedule_follow_up", "question": question,
            "choices": ["It arrived", "Still waiting"],
        })
        self.timing.return_value = json.dumps(timing)
        self.jobs.decide(anchor or self.now)
        return self.goals.detail(self.id)["follow_up"]

    def test_announces_the_saved_date_and_question_in_chat_before_delivery(self):
        self.goals.save(self.id, self.draft, self.now, language="en")
        question = "Did your replacement card arrive?"
        follow_up = self.schedule({"days_from_now": 7, "clock": "16:15"}, question)
        timing = json.loads(self.timing.call_args.args[0][1]["content"])
        self.assertEqual(timing["question"], question)
        self.generate.side_effect = [
            json.dumps({"message": f"I’ll ask you on July 2 at 9:00 AM. {question}"}),
            json.dumps({"message": f"July 8 at 4:15 PM. {question} Please confirm this time."}),
            json.dumps({"message": "I’ll ask you on [DAY] at [TIME]."}),
            "NO",
            json.dumps({"message": "On [DAY] at [TIME], I’ll ask whether your replacement card arrived."}),
            "YES",
        ]
        before = paged_chat_snapshot(self.store, None)
        self.notices.run(self.now)
        self.assertEqual(chat_history(self.store), [])
        now = self.now + timedelta(minutes=1)
        self.notices.run(now)
        self.notices.run(now)
        notice, = chat_history(self.store)
        snapshot = paged_chat_snapshot(self.store, before["cursor"])
        self.assertEqual(snapshot["items"], [notice])
        self.assertEqual(notice["text"], "On July 8 at 4:15 PM, I’ll ask whether your replacement card arrived.")
        self.assertNotIn("follow_up", notice)
        self.assertEqual(artifacts(self.store, self.id), [])
        data = json.loads(self.generate.call_args.args[0][1]["content"])
        self.assertEqual((data["day"], data["time"]), ("July 8", "4:15 PM"))
        self.assertEqual(data["question"], question)
        self.assertEqual(self.generate.call_count, 6)
        self.jobs.deliver(datetime.fromisoformat(follow_up["due"]))
        self.assertEqual(chat_history(self.store)[-1]["follow_up"]["question"], question)

    def test_announces_the_next_check_in_and_discards_an_edited_schedule_during_writing(self):
        goal = self.goals.save(self.id, self.draft, self.now)
        first = self.schedule({"start": "2026-07-08T16:15"}, "Did your replacement card arrive?")
        when = datetime.fromisoformat(first["due"])
        self.jobs.deliver(when)
        answer(self.store, self.id, first["id"], 1, when)
        self.schedule({"days_from_now": 5, "clock": "17:00"}, "Has the replacement card arrived?", anchor=when)

        def edit_during_generation(messages, _tokens):
            self.goals.save(self.id, self.draft | {
                "prompt": "Ask after the next postal delivery window.", "revision": goal["revision"],
            }, when)
            data = json.loads(messages[1]["content"])
            self.assertEqual((data["day"], data["time"]), ("July 13", "5:00 PM"))
            if "draft" in data:
                return "YES"
            return json.dumps({"message": "On [DAY] at [TIME], I’ll ask again about the card delivery."})

        self.generate.side_effect = edit_during_generation
        self.notices.run(when)
        item, = chat_history(self.store)
        self.assertEqual(item["id"], first["id"])
