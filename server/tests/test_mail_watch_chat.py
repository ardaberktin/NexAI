"""Exercise private Chat inbox checks, watch receipts, and cadence follow-ups."""

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from conversation_fixtures import action_claims, model, unlock
from nex.conversation.reply import WRITE, WRITE_MAIL_WATCH
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.conversation.verify_tool_reply import VERIFY_TOOL_REPLY, verified_tool_reply
from nex.goals import Goals
from nex.privacy import public
from nex.privacy.chat_history import ChatFollowups
from nex.privacy.requests import inbox
from nex.store import Store

REQUEST = "Check my emails for any UVic assignments I have and remind me of them whenever I have something."
TASK = "Watch Gmail for new UVic assignment emails and notify me of coursework that requires action."
SEARCH = {"name": "gmail", "query": "UVic assignments", "terms": ["Brightspace assignment"]}
WATCH = {"name": "goal", "query": TASK, "frequency": 60}
COMPOUND_ROUTE = '{"capabilities":["mail_check_and_watch"],"language":"en"}'
ZONE = "America/Vancouver"


class MailWatchChatTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.store = Store(root, 0, 2**20)
        self.goals = Goals(self.store)
        self.assistant = Mock(store=self.store, settings=None)
        self.session = unlock(self.store)
        self.followups = ChatFollowups()

    def ask(self, text, *, identifier=None, **options):
        params = {"session": self.session, "followups": self.followups}
        return public.chat(self.assistant, identifier or str(uuid4()), "mac", text, "en", ZONE, **(params | options))

    def seed_morning_goal(self):
        identifier = str(uuid4())
        task = "Report the day's appointments each morning."
        due = datetime.now(UTC) + timedelta(days=1)
        self.goals.save(identifier, {"prompt": task, "frequency_minutes": 1440, "scheduled_at": due.isoformat(), "zone": ZONE})
        self.followups.save("mac", self.session, (), identifier, task,
                            "The morning report will repeat daily at 8 AM.", private=True, goal_id=identifier)
        return identifier

    def test_compound_request_checks_existing_mail_and_saves_one_future_watch(self):
        morning = self.seed_morning_goal()
        goal_id = str(uuid4())
        evidence = []
        plans = []

        def plan(data):
            plans.append(data)
            self.assertEqual(data["question"], REQUEST)
            self.assertEqual(data["history"], [])
            return [SEARCH, WATCH] if "plan_feedback" in data else [SEARCH]

        def write(data):
            evidence.append(data["tools"])
            return "Your lab is due Friday. I saved an hourly watch for UVic assignments."

        self.assistant.generate_conversation = model(plan, goal_intent=COMPOUND_ROUTE, compose=write)
        rows = [{"subject": "Lab assignment", "sender": "Instructor", "body": "Submit by Friday."}]
        with patch("nex.conversation.tools.prepare_account", return_value=("Gmail", rows, "")) as read:
            self.assertIsNone(self.ask(REQUEST, identifier=goal_id, lookup_only=True))
            read.assert_not_called()
            self.assertEqual([row["id"] for row in self.goals.overview()], [morning])
            plans.clear()
            self.assertEqual(self.ask(REQUEST, identifier=goal_id)["goal_id"], goal_id)
            self.assertEqual(self.ask(REQUEST, identifier=goal_id)["goal_id"], goal_id)
            read.assert_called_once()
            self.assertEqual(read.call_args.args[1].search_variants, ("UVic assignments", "Brightspace assignment"))
        self.assertEqual(len(plans), 2)
        self.assertIn("gmail for the inbox search and goal", plans[-1]["plan_feedback"])
        self.assertEqual(evidence[0]["gmail"]["messages"][0]["excerpt"], "Submit by Friday.")
        self.assertEqual(evidence[0]["goal"]["frequency_minutes"], 60)
        goal = self.goals.detail(goal_id)
        self.assertEqual((goal["watch_provider"], goal["frequency_minutes"], goal["prompt"]), ("gmail", 60, TASK))
        self.assertEqual(len(goal["activity"]), 1)
        self.assertEqual(self.goals.detail(morning)["frequency_minutes"], 1440)
        self.assertEqual(inbox(self.store.privacy)["items"][0]["goal_id"], goal_id)

    def test_hourly_followup_creates_missing_watch_from_the_earlier_user_request(self):
        morning = self.seed_morning_goal()
        history = self.followups.previous("mac", self.session)
        self.followups.save("mac", self.session, history, str(uuid4()), REQUEST,
                            "Your next scheduled run is at 8 AM daily.", private=True)
        plan_context = []
        generate = model(lambda data: plan_context.append(data) or [WATCH],
                         goal_intent='{"capabilities":["schedule"],"language":"en"}', mail_action="mail_watch",
                         compose="I saved an hourly UVic assignment watch.")
        self.assistant.generate_conversation = generate

        with patch("nex.conversation.tools.prepare_account") as read:
            result = self.ask("Check it hourly")
            read.assert_not_called()
        route = json.loads(generate.call_args_list[0].args[0][1]["content"])
        self.assertTrue(generate.call_args_list[0].args[0][0]["content"].startswith(ROUTE_HEADER))
        self.assertIn(REQUEST, json.dumps(route["history"]))
        self.assertEqual(len(route["goal_references"]), 1)
        self.assertEqual(plan_context[0]["question"], "Check it hourly")
        self.assertIn(REQUEST, json.dumps(plan_context[0]["history"]))
        goal = self.goals.detail(result["goal_id"])
        remaining = datetime.fromisoformat(goal["next_run"]) - datetime.now(UTC)
        self.assertGreater(remaining, timedelta(minutes=58))
        self.assertLessEqual(remaining, timedelta(hours=1))
        self.assertEqual((goal["watch_provider"], goal["frequency_minutes"]), ("gmail", 60))
        self.assertEqual(self.goals.detail(morning)["frequency_minutes"], 1440)

    def test_empty_plan_does_not_publish_invented_search_and_schedule(self):
        self.seed_morning_goal()
        invented = "I found no assignments. The next check is tomorrow at 8 AM."
        revised = "I wasn't able to check the inbox or save an assignment watch."
        drafts = iter((invented, revised))
        reviewed = []

        def generate(messages, *_args, **_kwargs):
            prompt = messages[0]["content"]
            if prompt.startswith(ROUTE_HEADER):
                return '{"capabilities":["answer"],"language":"en"}'
            if prompt.startswith(WRITE):
                self.assertEqual(json.loads(messages[1]["content"])["tools"], {})
                return next(drafts)
            self.assertEqual(prompt, VERIFY_TOOL_REPLY)
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["question"], REQUEST)
            reviewed.append(data["draft"])
            return action_claims("checked", saved_watch=True) if data["draft"] == invented else action_claims("unavailable")

        self.assistant.generate_conversation = generate
        with patch("nex.conversation.tools.prepare_account") as read:
            result = self.ask(REQUEST)
            read.assert_not_called()
        self.assertNotIn("goal_id", result)
        self.assertEqual(reviewed, [invented, revised])
        self.assertEqual(inbox(self.store.privacy)["items"][0]["answer"], revised)
        self.assertEqual(len(self.goals.overview()), 1)

    def test_provider_error_and_saved_watch_reach_the_same_reply_as_independent_outcomes(self):
        provider_error = "Reconnect Gmail under Connected accounts in Nex Settings."
        generate = model([SEARCH, WATCH], goal_intent=COMPOUND_ROUTE,
                         compose="I saved the hourly watch. Reconnect Gmail so it can check your inbox.")

        def verify(messages, *args, **kwargs):
            if messages[0]["content"].startswith(WRITE_MAIL_WATCH):
                tools = json.loads(messages[1]["content"])["tools"]
                self.assertEqual(tools["gmail"], {"status": "unavailable", "detail": provider_error})
                self.assertEqual(tools["goal"]["frequency_minutes"], 60)
            return generate(messages, *args, **kwargs)

        self.assistant.generate_conversation = verify
        with patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [], provider_error)):
            result = self.ask(REQUEST)
        self.assertEqual(self.goals.detail(result["goal_id"])["watch_provider"], "gmail")
        self.assertIn("Reconnect Gmail", inbox(self.store.privacy)["items"][0]["answer"])

    def test_lookup_without_future_monitoring_does_not_save_a_watch(self):
        self.assistant.generate_conversation = model(
            [SEARCH], goal_intent=COMPOUND_ROUTE, mail_action="gmail",
            compose="The search returned no matches.",
        )
        with patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [], "")) as read:
            result = self.ask("Are there any emails in my inbox about UVic coursework?")
        read.assert_called_once()
        self.assertNotIn("goal_id", result)
        self.assertEqual(self.goals.overview(), [])

    def test_claim_verification_rejects_failed_reads_and_wrong_schedules(self):
        outcomes = {"gmail": {"status": "unavailable"},
                    "goal": {"schedule_text": "November 26 at 8:30 PM", "frequency_minutes": 60}}
        claim_sets = (
            (action_claims("checked", saved_watch=True, cadence=60), False),
            (action_claims("unavailable", saved_watch=True, cadence=1440), False),
            (action_claims("unavailable", saved_watch=True, cadence=60, next_run_matches=False), False),
            (action_claims("unavailable", saved_watch=True, cadence=60, instant_detection=True), False),
            (action_claims("unavailable", saved_watch=True, cadence=60), True),
        )
        for claims, expected in claim_sets:
            with self.subTest(claims=claims):
                generate = AsyncMock(return_value=claims)
                result = asyncio.run(verified_tool_reply(REQUEST, "Draft", outcomes, "2026-11-26T19:30-07:00", generate))
                self.assertIs(result, expected)
