import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from time import time
from unittest.mock import Mock
from uuid import uuid4

from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goal_follow_up_notices import GoalFollowUpNotices
from nex.goal_follow_ups import answer
from nex.goals import Goals
from nex.privacy.requests import inbox
from nex.store import Store
from nex.workspace_chat_pages import chat_page


class GoalChatReceiptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(folder.name, 0, 2**20)
        self.goal = str(uuid4())
        self.start = datetime.now(UTC)
        self.due = self.start + timedelta(days=3)
        self.question = "Have you picked up the keys?"
        self.draft = {"prompt": "Ask if I picked up the keys.", "zone": "UTC", "scheduled_at": self.due.isoformat()}
        self.decision = {"name": "schedule_follow_up", "use_goal_run": True, "question": self.question,
                         "choices": ["I have them", "Still waiting"]}
        self.sentence = "On [DAY] at [TIME], I’ll ask whether you’ve picked up the keys."

    def private_turn(self, request, draft):
        with self.store.privacy.transaction(write=True) as db:
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,'ready',?,?)",
                       (request, "mac", json.dumps({"text": self.draft["prompt"]}), "", time(), time(), draft))
            db.execute("INSERT INTO request_goals VALUES(?,?)", (request, self.goal))

    def test_upgrade_merges_an_existing_pair_without_changing_the_reminder(self):
        goals = Goals(self.store)
        goals.save(self.goal, self.draft, self.start, request=self.draft["prompt"], check_in=True)
        self.private_turn(self.goal, "The goal is saved. A time has not been set.")
        GoalFollowUpJobs(self.store, Mock(return_value=json.dumps(self.decision))).decide(self.start)
        saved = goals.detail(self.goal)["follow_up"]
        with self.store.connect() as db:
            db.execute("UPDATE goal_follow_up_notices SET text=?,created=?,next_try=NULL",
                       ("Scheduled.\n\n" + self.question, self.start.isoformat()))
            for table, index in (("goal_follow_up_notices", "goal_notice_request"),
                                 ("goal_follow_up_work", "goal_work_request")):
                db.execute(f"DROP INDEX {index}")
                db.execute(f"ALTER TABLE {table} DROP COLUMN chat_request_id")

        upgraded = Store(self.store.root, 0, 2**20)
        self.assertEqual(Goals(upgraded).detail(self.goal)["follow_up"], saved)
        self.assertEqual(chat_page(upgraded)["items"], [])
        item, = inbox(upgraded.privacy)["items"]
        self.assertEqual((item["state"], item["answer"]), ("working", None))
        GoalFollowUpNotices(upgraded, Mock(side_effect=[json.dumps({"message": self.sentence}), "YES"])).run()
        item, = inbox(upgraded.privacy)["items"]
        self.assertEqual((item["state"], item["id"], item["goal_id"]), ("ready", self.goal, self.goal))
        self.assertIn("I’ll ask whether you’ve picked up the keys.", item["answer"])
        self.assertEqual(chat_page(upgraded)["items"], [])
        goals.delete(self.goal, goals.detail(self.goal)["revision"])
        self.assertEqual(inbox(upgraded.privacy)["items"][0]["answer"], item["answer"])

    def test_edit_and_later_answer_have_separate_confirmation_owners(self):
        goals = Goals(self.store)
        first = goals.save(self.goal, self.draft, self.start, check_in=True, chat_request_id=self.goal)
        self.private_turn(self.goal, "Initial goal saved.")
        edit = str(uuid4())
        goals.save(self.goal, self.draft | {"revision": first["revision"], "prompt": "Ask about the office keys."},
                   self.start, check_in=True, chat_request_id=edit)
        self.private_turn(edit, "The edited goal is saved.")
        jobs = GoalFollowUpJobs(self.store, Mock(return_value=json.dumps(self.decision)))
        jobs.decide(self.start)
        notices = GoalFollowUpNotices(self.store, Mock(side_effect=[json.dumps({"message": self.sentence}), "YES"]))
        notices.run(self.start)
        replies = {item["id"]: item["answer"] for item in inbox(self.store.privacy)["items"]}
        self.assertEqual(replies[self.goal], "Initial goal saved.")
        self.assertIn("picked up the keys", replies[edit])
        self.assertEqual(chat_page(self.store)["items"], [])
        jobs.deliver(self.due)
        card, = chat_page(self.store)["items"]
        answer(self.store, self.goal, card["id"], 1, self.due)
        jobs.generate = Mock(side_effect=[json.dumps(self.decision | {"use_goal_run": False}),
                                          '{"days_from_now":2,"clock":"14:00"}'])
        jobs.decide(self.due)
        notices.generate = Mock(side_effect=[json.dumps({"message": self.sentence}), "YES"])
        notices.run(self.due)
        self.assertEqual(len(chat_page(self.store)["items"]), 2)
        self.assertEqual(replies, {item["id"]: item["answer"] for item in inbox(self.store.privacy)["items"]})
