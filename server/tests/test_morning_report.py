import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from time import time
from unittest.mock import Mock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from conversation_fixtures import goal_clock, model
from nex.assistant import Assistant
from nex.goal_schedule import DEFAULT_TIME, next_local_run
from nex.goals import Goals
from nex.privacy import public, requests
from nex.privacy.state import digest
from nex.settings import Settings
from nex.store import Store
from nex.weather import Place, Weather
from nex.workspace_content import chat_history

QUESTION = ("Every morning send me a morning report of what I have to do that day, what the weather is like "
            "any anything important that I should now.")
ZONE = "America/Vancouver"


class MorningReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(folder.name, 0, 2**20)
        self.goals = Goals(self.store)
        self.identifier = str(uuid4())
        self.model = Mock()
        self.assistant = Assistant(self.store, self.model, Settings(folder.name))
        with self.store.privacy.transaction(write=True) as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest("open"), "mac", time() + 60))

    def ask(self, text=QUESTION):
        return public.chat(self.assistant, self.identifier, "mac", text, "en", ZONE, session="open")

    def test_ai_saves_the_report_at_the_default_time_without_a_mailbox_watch(self):
        start = next_local_run(DEFAULT_TIME, ZONE, datetime.now(UTC))
        self.model.generate = model([{"name":"goal", "query":QUESTION, "frequency":1440, "start":goal_clock(start, ZoneInfo(ZONE))}],
                                    compose="Your report is scheduled for 8:00 AM every day.")
        self.assertEqual(self.ask()["goal_id"], self.identifier)
        self.ask()
        goal = self.goals.detail(self.identifier)
        local = datetime.fromisoformat(goal["scheduled_at"]).astimezone(ZoneInfo(ZONE))
        self.assertEqual((local.strftime("%H:%M"), goal["frequency_minutes"], goal["prompt"]),
                         (DEFAULT_TIME, 1440, QUESTION))
        self.assertEqual(len(goal["activity"]), 1)
        answer, = requests.inbox(self.store.privacy)["items"]
        self.assertIn("8:00 AM", answer["answer"])
        self.assertIn("every day", answer["answer"])
        self.model.classify.assert_not_called()

    def test_gmail_classifier_cannot_create_a_watch_without_confirming_the_source(self):
        self.model.classify.side_effect = ["N", "Y", "N", "SKIP"]
        self.model.generate.return_value = "Please clarify the requested report."
        self.assertNotIn("goal_id", self.ask())
        self.assertEqual(self.goals.overview(), [])

    def test_due_report_reads_integrations_and_posts_a_single_shared_update(self):
        anchor = datetime(2026, 11, 27, 13, tzinfo=UTC)
        start = next_local_run("08:30", ZONE, anchor)
        self.goals.save(self.identifier, {"prompt": QUESTION, "zone": ZONE, "frequency_minutes": 1440,
                                          "scheduled_at": start.isoformat()}, anchor=anchor)
        due = datetime.fromisoformat(self.goals.detail(self.identifier)["next_run"])
        self.goals.save(str(uuid4()), {"prompt": "Call a classmate", "zone": ZONE,
                                       "scheduled_at": (due + timedelta(hours=2)).isoformat()}, anchor=anchor)
        Weather(self.store).remember("mac", Place("Victoria", "British Columbia", "Canada", 48.4, -123.4))
        responses = {
            "calendar": ("Google Calendar", [{"title": "Lab meeting", "start": "2026-11-27 10:00"}], ""),
            "ticktick": ("TickTick", [{"title": "Submit worksheet", "due": "2026-11-27 15:00"}], ""),
            "gmail": ("Gmail", [
                {"subject": "Club newsletter", "body": "Read our club news."},
                {"subject": "Lab report due today", "sender": "Course team",
                 "body": "Upload the lab PDF to Brightspace by 5 pm today. Reply if you need an extension."},
            ], ""),
        }

        def prepare(root, intent, query, zone, reference):
            self.assertEqual(reference, due)
            if intent.provider == "gmail":
                self.assertTrue(intent.unread and intent.include_body and intent.priority)
            return responses[intent.provider]

        offer = "Want me to draft a note to the course team asking for an extension?"
        summary = "Upload the lab PDF to Brightspace by 5 pm today."
        generate = Mock(side_effect=[
            ('{"tools":[{"name":"agenda","query":"today"},{"name":"weather"},'
             '{"name":"gmail","unread":true,"priority":true}]}'),
            "2", ("Lab meeting at 10. Submit worksheet at 15:00. Call a classmate. "
                  f"It is 9 degrees Celsius in Victoria. Lab report due today from Course team. {summary}\n\n{offer}"),
        ])
        goals = Goals(self.store, generate=generate)
        with self.store.connect() as db:
            db.execute("INSERT INTO iphone_push_registration VALUES(1,?,?,?)", (str(uuid4()), "ab" * 32, "development"))
        with (patch("nex.accounts.agenda.prepare_account", side_effect=prepare),
              patch("nex.goal_runner.prepare_account", side_effect=prepare),
              patch("nex.weather.current_conditions", return_value=(3, 9, 8))):
            goals.run(due)
            goals.run(due + timedelta(minutes=1))
        update, = chat_history(self.store)
        for expected in ("Submit worksheet", "Lab meeting", "Call a classmate", "9 degrees Celsius in Victoria",
                         "Lab report due today", "Course team", "5 pm today", "Brightspace", offer):
            self.assertIn(expected, update["text"])
        evidence = json.loads(generate.call_args.args[0][1]["content"])["evidence"]
        self.assertCountEqual([item["title"] for item in evidence["agenda"]["items"]],
                              ["Lab meeting", "Submit worksheet", "Call a classmate"])
        selected, = evidence["gmail"]
        self.assertIn("Reply if you need an extension", selected["excerpt"])
        self.assertEqual(update["goal_id"], self.identifier)
        with self.store.connect() as db:
            self.assertEqual([row["id"] for row in db.execute("SELECT id FROM goal_push_delivery")], [update["id"]])

    def test_recurrence_keeps_its_local_time_across_daylight_saving(self):
        anchor = datetime(2026, 10, 31, 15, tzinfo=UTC)
        self.assertEqual(next_local_run("08:00", "America/Los_Angeles", anchor),
                         datetime(2026, 11, 1, 16, tzinfo=UTC))
