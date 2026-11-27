import json
import tempfile
import unittest
from datetime import UTC, datetime, time, timedelta
from time import time as unix_time
from unittest.mock import Mock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from conversation_fixtures import goal_clock, model
from nex.assistant import Assistant
from nex.coding.sandbox import Run
from nex.conversation.tools import Evidence
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goal_follow_up_notices import GoalFollowUpNotices
from nex.goal_requests import goal_request, resolve_reminder
from nex.goals import Goals
from nex.privacy import public, requests
from nex.privacy.chat_history import ChatFollowups
from nex.privacy.state import digest
from nex.settings import Settings
from nex.store import Store
from nex.workspace_chat_pages import chat_page
from nex.workspace_content import chat_history

ZONE = "America/Vancouver"


class NaturalGoalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name, 0, 2**20)
        self.model = Mock()
        self.model.classify.return_value = None
        self.assistant = Assistant(self.store, self.model, Settings(self.store.root))
        self.identifier = str(uuid4())
        self.goals = Goals(self.store)
        with self.store.privacy.transaction(write=True) as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest("open"), "iphone", unix_time() + 60))

    def schedule(self, prompt, minutes=0, start=None, **options):
        call = {"name": "goal", "query": prompt, "frequency": minutes, **options}
        if start:
            call["start"] = goal_clock(start, ZoneInfo(ZONE))
        self.model.generate = model([call], compose="I created the requested goal.")

    def ask(self, text, **options):
        return public.chat(self.assistant, self.identifier, "iphone", text, "en", ZONE, **options)

    def test_mail_delivery_reminder_after_weather_saves_one_private_check_in(self):
        followups = ChatFollowups()
        options = {"followups": followups, "session": "open"}
        weather = "The weather today was mostly overcast with a low of 3.3 and a high of 7.3 degrees Celsius."
        self.assistant.generate_conversation = model([{"name": "weather", "period": "current"}], compose=weather)
        with patch("nex.conversation.tools.weather_result", return_value=Evidence({}, engine="weather")):
            self.assertEqual(self.ask("How's the weather outside?", **options)["text"], weather)
        self.assertEqual(followups.previous("iphone", "open")[-1]["text"], weather)

        self.identifier = str(uuid4())
        text = "can you remind me i’m waiting for my replacement library card to come in the mail"
        check_in = "Ask whether my replacement library card has arrived"
        first_plan = [{"name": "agenda", "query": "today"}]

        def plan(data):
            self.assertEqual(data["question"], text)
            self.assertEqual(data["history"][-1]["content"], weather)
            if "first_choice" not in data:
                return first_plan
            if "plan_feedback" in data:
                self.assertIn("quote the requested time", data["plan_feedback"])
                return [{"name": "goal", "query": check_in, "check_in": True}]
            return [{"name": "goal", "query": check_in, "start": "2025-04-11T08:00"}]

        def compose(data):
            self.assertNotIn("history", data)
            receipt = data["tools"]["goal"]
            self.assertEqual(receipt["prompt"], check_in)
            self.assertIsNone(receipt["schedule_text"])
            self.assertTrue(receipt["check_in"])
            return "I saved your library card check-in."

        self.assistant.generate_conversation = model(plan, compose=compose)
        with patch("nex.conversation.tools.agenda_evidence", return_value={"items": []}) as agenda:
            self.assertIsNone(self.ask(text, lookup_only=True, **options))
            self.assertFalse(self.goals.overview())
            self.assertEqual(self.ask(text, **options)["goal_id"], self.identifier)
            self.assertEqual(self.ask(text, **options)["goal_id"], self.identifier)
        agenda.assert_not_called()
        goal = self.goals.detail(self.identifier)
        item, = requests.inbox(self.store.privacy)["items"]
        self.assertIsNone(goal["scheduled_at"])
        self.assertEqual(goal["frequency_minutes"], 0)
        self.assertEqual(len(goal["activity"]), 1)
        self.assertEqual(goal["prompt"], check_in)
        self.assertEqual((item["state"], item["answer"]), ("working", None))

    def test_untimed_check_in_gets_one_private_confirmation_then_the_question_when_due(self):
        self.schedule("Ask whether my library card arrived", check_in=True)
        self.ask("Remind me to check if my library card came", session="open")
        when = datetime.now(UTC)
        jobs = GoalFollowUpJobs(self.store, Mock(side_effect=[
            '{"name":"schedule_follow_up","question":"Did your new library card arrive?","choices":["Yes","Not yet"]}',
            '{"days_from_now":3,"clock":"16:25"}',
        ]))
        jobs.decide(when)
        follow_up = self.goals.detail(self.identifier)["follow_up"]
        due = datetime.fromisoformat(follow_up["due"]).astimezone(ZoneInfo(ZONE))
        notice = GoalFollowUpNotices(self.store, Mock(side_effect=[
            '{"message":"On [DAY] at [TIME], I’ll ask whether your library card has arrived."}', "YES",
        ]))
        notice.run(when)
        notice.run(when)
        item, = requests.inbox(self.store.privacy)["items"]
        self.assertEqual((item["id"], item["goal_id"], item["state"]), (self.identifier, self.identifier, "ready"))
        expected = f"On {due:%B} {due.day} at 4:25 PM, I’ll ask whether your library card has arrived."
        self.assertEqual(item["answer"], expected)
        self.assertFalse(any(row.get("goal_id") for row in chat_page(self.store)["items"]))
        self.assertFalse(any(row.get("goal_id") for row in chat_history(self.store)))
        jobs.deliver(due)
        card, = [row for row in chat_page(self.store)["items"] if row.get("goal_id")]
        self.assertEqual(card["text"], "Did your new library card arrive?")
        self.assertTrue(card["follow_up"]["can_answer"])

    def test_model_review_can_ask_for_clarification_without_saving_a_reminder(self):
        def plan(data):
            return [] if "first_choice" in data else [{"name": "agenda", "query": "today"}]

        self.assistant.generate_conversation = model(plan, compose="When would you like me to remind you?")
        with patch("nex.conversation.tools.agenda_evidence", return_value={"items": []}) as agenda:
            self.assertNotIn("goal_id", self.ask("Remind me to check my delivery", session="open"))
        agenda.assert_not_called()
        self.assertFalse(self.goals.overview())
        item, = requests.inbox(self.store.privacy)["items"]
        self.assertEqual(item["answer"], "When would you like me to remind you?")

    def test_conditional_repeat_uses_the_verified_goal_and_keeps_the_existing_check_in(self):
        followups = ChatFollowups()
        options = {"followups": followups, "session": "open"}
        text = "Can you remind me I’m waiting for my card to come in the mail?"
        target = self.identifier
        self.model.generate = model(
            [{"name": "goal", "query": "Ask if my card arrived", "check_in": True}],
            compose="I’ll ask at the end of 2099.",
        )
        self.assertEqual(self.ask(text, **options)["goal_id"], target)

        jobs = GoalFollowUpJobs(self.store, Mock(side_effect=[
            '{"name":"schedule_follow_up","question":"Did your card arrive?","choices":["Yes","Still waiting"]}',
            '{"days_from_now":5,"clock":"17:45"}',
        ]))
        jobs.decide()
        initial = self.goals.detail(target)["follow_up"]
        self.assertIsNotNone(initial)
        self.identifier = str(uuid4())

        def plan(data):
            reference, = data["goal_references"]
            self.assertEqual(reference["request"], text)
            saved_local = datetime.fromisoformat(initial["due"]).astimezone(ZoneInfo(ZONE))
            self.assertEqual(reference["next_check_in"], saved_local.isoformat(timespec="minutes"))
            self.assertNotIn("2099", json.dumps(data["history"]))
            return [{"name": "goal", "target": reference["target"], "check_in": True}]

        self.model.generate = model(plan, compose="I saved that check-in on the existing goal.")
        reply = self.ask("Make that recurring if it doesn't arrive by then", **options)
        self.assertEqual(reply["goal_id"], target)
        self.assertEqual([item["id"] for item in self.goals.overview()], [target])
        self.assertEqual(self.goals.detail(target)["follow_up"], initial)

    def test_uvic_email_request_saves_an_hourly_watch(self):
        text = "Remind me about my UVic emails"
        self.schedule("Watch my UVic emails", 60, datetime.now(UTC) + timedelta(hours=1))
        self.assertIsNone(self.ask(text, session="open", lookup_only=True))
        self.assertFalse(self.goals.overview())
        self.assertEqual(self.ask(text, session="open")["goal_id"], self.identifier)
        goal = self.goals.detail(self.identifier)
        self.assertEqual(goal["frequency_minutes"], 60)
        self.assertIsNotNone(goal["scheduled_at"])
        self.assertIn("my UVic emails", goal["prompt"])
        self.model.classify.assert_not_called()

    def test_compound_mailbox_request_saves_an_assignment_watch_without_python(self):
        text = "check in on my uvic emails and remind me of any assignments"
        self.schedule(text, 60, datetime.now(UTC) + timedelta(hours=1))
        with patch("nex.coding.answers.run_python", return_value=Run("ok", "No upcoming assignments found.")) as run:
            self.assertIsNone(self.ask(text, session="open", lookup_only=True))
            self.assertFalse(self.goals.overview())
            result = self.ask(text, session="open")
            self.assertEqual((result["engine"], result["goal_id"]), ("private_request", self.identifier))
            self.ask(text, session="open")
        self.model.classify.assert_not_called()
        run.assert_not_called()
        goal = self.goals.detail(self.identifier)
        self.assertEqual(goal["frequency_minutes"], 60)
        self.assertIn(text, goal["prompt"])
        self.assertEqual(len(goal["activity"]), 1)
        item, = requests.inbox(self.store.privacy)["items"]
        self.assertIn("I created", item["answer"])
        self.assertEqual(chat_history(self.store), [])

    def test_semantic_mail_read_and_unavailable_router_do_not_fall_through_to_python(self):
        text = "see if any assignments came through my uvic emails"
        self.model.generate = model([{"name":"gmail","query":"uvic assignments"}], compose="Real mailbox result")
        with patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [], "empty")) as read:
            self.ask(text, session="open")
            self.assertEqual(read.call_args.args[1].topic, "uvic assignments")
        self.assertFalse(self.goals.overview())
        self.identifier = str(uuid4())
        self.model.generate.return_value = self.model.generate_warm.return_value = None
        self.model.generate.side_effect = None
        with patch("nex.coding.answers.run_python") as run:
            unavailable = self.ask(text, session="open")
            run.assert_not_called()
        self.assertEqual((unavailable["engine"], unavailable["text"]), ("model_unavailable", ""))
        self.assertEqual(chat_history(self.store), [])
        self.assertEqual([row["answer"] for row in requests.inbox(self.store.privacy)["items"]],
                         ["Real mailbox result"])

    def test_approved_request_reuses_the_models_decision(self):
        text = "Please skim my campus correspondence and give me a heads up about homework notices"
        self.schedule(text, 60, datetime.now(UTC) + timedelta(hours=1))
        self.assertEqual(self.ask(text)["engine"], "private_request")
        self.assertFalse(self.goals.overview())
        calls_before_approval = self.model.generate.call_count
        self.assertEqual(self.ask(text, session="open")["goal_id"], self.identifier)
        self.assertIn(text, self.goals.detail(self.identifier)["prompt"])
        self.assertEqual(self.model.generate.call_count, calls_before_approval + 2)

    def test_ordinary_math_can_use_python_after_the_model_rejects_a_reminder(self):
        self.model.classify.side_effect = ["N", "N", "C", "N", "RUN"]
        self.model.generate = model([{"name":"python"}], code="print(86.4 / 6)")
        question = "The bill for my dinner was $86.40 for six people. What does each person owe?"
        with patch("nex.coding.answers.run_python", return_value=Run("ok", "14.40")):
            self.assertEqual(self.ask(question, session="open")["engine"], "python_sandbox")
        self.assertFalse(self.goals.overview())

    def test_package_gets_one_check_in_when_due_after_private_approval(self):
        text = "Remind me in a week at 9 a.m. to check whether my package arrived"
        due = datetime.now(ZoneInfo(ZONE)) + timedelta(days=7)
        self.model.generate = model([{"name":"goal", "query":"Did you receive your package in the mail?",
                                     "check_in": True, "timing": "in a week at 9 a.m.",
                                     "start":goal_clock(due.replace(hour=9, minute=0), ZoneInfo(ZONE))}],
                                    compose="Scheduled for 9:00 AM your time.")
        self.assertEqual(self.ask(text)["engine"], "private_request")
        self.assertFalse(self.goals.overview())
        requests.resolve(self.store.privacy, "iphone", "open", self.identifier, self.assistant, None)
        goal = self.goals.detail(self.identifier)
        question = "Did you receive your package in the mail?"
        self.assertIn(question, goal["prompt"])
        self.assertEqual((goal["frequency_minutes"], goal["zone"]), (0, ZONE))
        item, = requests.inbox(self.store.privacy)["items"]
        local = datetime.fromisoformat(item["asked_at"]).astimezone(ZoneInfo(ZONE))
        due = datetime.fromisoformat(goal["scheduled_at"])
        self.assertEqual(due.astimezone(local.tzinfo),
                         datetime.combine(local.date() + timedelta(days=7), time(9), local.tzinfo))
        self.assertEqual(item["state"], "working")
        self.assertEqual(item["goal_id"], self.identifier)
        with self.store.connect() as db:
            db.execute("INSERT INTO iphone_push_registration VALUES(1,?,?,?)", (str(uuid4()), "ab" * 32, "development"))
        call = {"name": "schedule_follow_up", "use_goal_run": True, "question": question,
                "choices": ["It was delivered", "I'm still waiting"]}
        run = Mock()
        Goals(self.store, generate=run).run(due)
        run.assert_not_called()
        jobs = GoalFollowUpJobs(self.store, Mock(return_value=json.dumps(call)))
        jobs.decide()
        GoalFollowUpNotices(self.store, Mock(side_effect=[
            '{"message":"I’ll ask whether your parcel arrived on [DAY] at [TIME]."}', "YES",
        ])).run()
        self.assertIn("9:00 AM", requests.inbox(self.store.privacy)["items"][0]["answer"])
        jobs.deliver(due - timedelta(seconds=1))
        self.assertEqual(chat_history(self.store), [])
        jobs.deliver(due)
        jobs.deliver(due + timedelta(minutes=1))
        update, = chat_history(self.store)
        self.assertEqual((update["goal_id"], update["text"]), (self.identifier, question))
        self.assertEqual(update["follow_up"]["choices"], call["choices"])
        with self.store.connect() as db:
            self.assertEqual([row["id"] for row in db.execute("SELECT id FROM goal_push_delivery")], [update["id"]])
        self.assertIsNone(self.goals.detail(self.identifier)["next_run"])
        self.model.classify.assert_not_called()

    def test_local_model_can_infer_an_implied_email_watch(self):
        self.schedule("Watch new Gmail messages for scholarship replies", 60, datetime.now(UTC) + timedelta(hours=1))
        self.ask("Could you nudge me when the scholarship committee gets back to me?", session="open")
        goal = self.goals.detail(self.identifier)
        self.assertEqual(goal["frequency_minutes"], 60)
        self.assertIn("new Gmail messages", goal["prompt"])
        self.model.classify.assert_not_called()

    def test_future_event_has_a_check_in_when_the_model_only_sees_a_task(self):
        self.schedule("Ask a check-in question about the repair", check_in=True)
        self.ask("Remind me once the repair is finished", session="open")
        goal = self.goals.detail(self.identifier)
        self.assertIn("check-in question", goal["prompt"])
        self.assertIsNone(goal["scheduled_at"])
        generate = Mock(side_effect=[
            '{"name":"leave_as_is"}',
            '{"name":"schedule_follow_up","question":"Is the repair finished?","choices":["Fixed","Still in repair"]}',
            '{"start":"2025-04-11T08:00"}',
            '{"days_from_now":3,"clock":"16:30"}',
        ])
        GoalFollowUpJobs(self.store, generate).decide()
        self.assertIsNotNone(self.goals.detail(self.identifier)["follow_up"])

    def test_reminder_times_resolve_in_the_device_time_zone(self):
        local = datetime(2026, 10, 29, 11, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        for text, subject, due in (
            ("Check in with me in a week or so to see if I got my parcel", "Did you receive your parcel?",
             datetime(2026, 11, 5, 19, 30, tzinfo=UTC)),
            ("Remind me tomorrow at 2 p.m. to read my UVic emails", "read my UVic emails",
             datetime(2026, 10, 30, 21, tzinfo=UTC)),
            ("Remind me to take out the recycling in 15 minutes", "take out the recycling",
             datetime(2026, 10, 29, 18, 45, tzinfo=UTC)),
            ("Tomorrow, remind me to pay the parking ticket", "pay the parking ticket",
             datetime(2026, 10, 30, 16, tzinfo=UTC)),
        ):
            with self.subTest(text=text):
                goal = resolve_reminder(goal_request(text), local, None)
                self.assertTrue(goal.prompt.endswith(subject))
                self.assertEqual(goal.scheduled_at, due)

    def test_recall_and_immediate_requests_do_not_create_goals(self):
        self.assertIsNone(goal_request("Remind me what we agreed to last week"))
        self.assertIsNone(goal_request("Remind me when my exam is"))
        self.assertIsNone(goal_request("Let me know what you think"))
        self.assertIsNone(goal_request("Check if I have any UVic emails"))

    def test_an_untimed_task_to_send_email_stays_a_personal_goal(self):
        self.schedule("send an email to my professor")
        self.ask("Remind me to send an email to my professor", session="open")
        goal = self.goals.detail(self.identifier)
        self.assertNotIn("kind", goal)
        self.assertEqual((goal["prompt"], goal["next_run"]), ("send an email to my professor", None))
        self.model.classify.assert_not_called()

    def test_chat_can_schedule_a_recurring_prompt_with_a_specific_time(self):
        start = (datetime.now(ZoneInfo(ZONE)) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
        self.schedule("suggest a writing exercise", 1440, start)
        self.ask("Create a goal to suggest a writing exercise every morning at 8:30 a.m.", session="open")
        goal = self.goals.detail(self.identifier)
        due = datetime.fromisoformat(goal["scheduled_at"]).astimezone(ZoneInfo(ZONE))
        self.assertEqual((goal["prompt"], goal["frequency_minutes"], due.time()),
                         ("suggest a writing exercise", 1440, time(8, 30)))

    def test_invalid_goal_plan_returns_private_clarification_instead_of_failing_inbox_item(self):
        text = "remind me that i'm waiting for my walmart credit card to arrive in the mail"
        clarification = "I didn't save a reminder yet—would you like me to check in later about your Walmart credit card?"

        def generate(messages, *_args, **_kwargs):
            prompt = messages[0]["content"]
            if prompt.startswith("Select capabilities"):
                return '{"capabilities":["check_in"],"language":"en"}'
            if prompt.startswith("Plan the requested personal outcome check-in"):
                # Both attempts propose an unquoted start time, triggering invalid_goal_plan.
                return json.dumps({"tools": [{"name": "goal", "query": "Ask about card",
                                              "check_in": True, "start": "2027-01-15T09:00"}]})
            if prompt.startswith("In one or two short sentences, explain the goal outcome"):
                return clarification
            if prompt.startswith("Return YES if the draft is consistent with the tool outcome"):
                return "YES"
            raise AssertionError(f"Unexpected prompt: {prompt[:60]}")

        self.assistant.generate_conversation = Mock(side_effect=generate)
        result = self.ask(text, session="open")
        self.assertEqual(result["engine"], "private_request")
        self.assertNotIn("goal_id", result)
        self.assertFalse(self.goals.overview())
        item, = requests.inbox(self.store.privacy)["items"]
        self.assertEqual((item["state"], item["answer"]), ("ready", clarification))
