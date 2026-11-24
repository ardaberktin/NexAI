import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

from conversation_fixtures import unlock
from nex.assistant import Assistant
from nex.conversation.document_routing import CHOOSE_DOCUMENT_ACTION
from nex.conversation.goal_management_prompts import (
    EDIT_GOAL,
    FIND_PDF_GOALS,
    SELECT_PDF_GOAL,
    VERIFY_PDF_CHANGE,
    VERIFY_PDF_TARGET,
)
from nex.conversation.reply import (
    VERIFY_GOAL_FORMAT,
    WRITE_GOAL_CLARIFICATION,
    WRITE_GOAL_FORMAT,
)
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goal_follow_up_pdfs import deliver_pdfs
from nex.goals import Goals
from nex.pdf.document import WRITE_DOCUMENT
from nex.privacy import public, requests
from nex.privacy.chat_history import ChatFollowups
from nex.store import Store
from nex.workspace_content import artifacts, chat_history


class GoalPDFEditTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(self.root, 0, 2**20)
        self.goals = Goals(self.store)
        self.anchor = datetime.now(UTC).replace(second=0, microsecond=0)
        self.start = self.anchor + timedelta(hours=2)
        self.instruction = "Attach a one-column PDF to every update."

    def save(self, title, prompt, **options):
        return self.goals.save(
            str(uuid4()), {"title": title, "prompt": prompt, "zone": "UTC", "scheduled_at": self.start.isoformat()},
            anchor=self.anchor, **options,
        )

    def test_ambiguous_request_asks_in_private_and_follow_up_edits_only_the_selected_goal(self):
        first = self.save("Garden check", "Report on the greenhouse.")
        second = self.save("Kitchen check", "Report on the pantry.")
        unrelated = [self.save(f"Library pickup {index}", "Remind me to pick up a book.") for index in range(7)]
        initial = "Add a PDF to each of my check-ins."
        clarification = "Which goal should get a PDF, Garden check or Kitchen check?"
        follow_up = "garden check"
        assistant = Assistant(self.store, None, None)
        followups = ChatFollowups()
        identifiers = [str(uuid4()), str(uuid4())]
        asked = initial
        attempted_targets = []

        def generate(messages, *_args, **_options):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            if prompt.startswith(ROUTE_HEADER):
                self.assertEqual(data["latest_message"], asked)
                self.assertNotIn("goals", data)
                return "MANAGE" if asked == follow_up else "DOCUMENT"
            if prompt == CHOOSE_DOCUMENT_ACTION:
                if asked == follow_up:
                    self.assertEqual([msg["content"] for msg in data["history"]], [initial, clarification])
                return "FORMAT"
            if prompt == FIND_PDF_GOALS:
                self.assertEqual(asked, initial)
                titles = {first["title"], second["title"], unrelated[0]["title"]}
                return json.dumps({"matches": [row["title"] in titles for row in data["goals"]]})
            if prompt == SELECT_PDF_GOAL and asked == initial:
                self.assertEqual(len(data["goals"]), 3)
                unrelated_target, = [row["target"] for row in data["goals"] if row["title"] == unrelated[0]["title"]]
                attempted_targets.append(unrelated_target)
                return json.dumps({"action": "clarify", "targets": [
                    row["target"] for row in data["goals"] if row["target"] != unrelated_target
                ]})
            if prompt.startswith(SELECT_PDF_GOAL):
                self.assertCountEqual([row["title"] for row in data["goals"]], [first["title"], second["title"]])
                if len(attempted_targets) == 1:
                    attempted_targets.append(None)
                    return json.dumps({"target": attempted_targets[0], "title": unrelated[0]["title"]})
                chosen_title = second["title"] if len(attempted_targets) == 2 else first["title"]
                choice, = [row for row in data["goals"] if row["title"] == chosen_title]
                attempted_targets.append(choice["target"])
                return json.dumps({key: choice[key] for key in ("target", "title")})
            if prompt == VERIFY_PDF_TARGET:
                self.assertEqual(data["latest_message"], follow_up)
                self.assertEqual(data["selected_goal"]["title"], first["title"])
                self.assertEqual(data["other_candidates"][0]["title"], second["title"])
                return "YES"
            if prompt.startswith(EDIT_GOAL):
                self.assertEqual(data["clarification"][0]["content"], initial)
                if "Correct the previous edit" not in prompt:
                    return json.dumps({"format_instruction": self.instruction, "frequency": 60})
                self.assertIn("Only a nonempty format_instruction may change", prompt)
                return json.dumps({"format_instruction": self.instruction})
            if prompt == VERIFY_PDF_CHANGE:
                self.assertEqual(data["preceding_clarification"][0]["content"], initial)
                return "YES"
            if prompt == WRITE_GOAL_FORMAT:
                self.assertEqual(data["tools"]["goal"]["title"], first["title"])
                return "I saved PDF output for Kitchen check."
            if prompt.startswith(WRITE_GOAL_FORMAT):
                return "I saved PDF output for Garden check."
            if prompt == VERIFY_GOAL_FORMAT:
                self.assertIn("Garden check", data["draft"])
                return "YES"
            self.assertEqual(prompt, WRITE_GOAL_CLARIFICATION)
            self.assertCountEqual(data["candidate_titles"], [first["title"], second["title"]])
            self.assertEqual(data["request"], initial)
            return json.dumps({"question": clarification})

        def ask(identifier, session=None):
            return public.chat(assistant, identifier, "mac", asked, "en", "UTC", followups=followups, session=session)

        assistant.generate_conversation = Mock(side_effect=generate)
        self.assertEqual(ask(identifiers[0])["engine"], "private_request")
        # Planning before unlock may classify the action but may not read the saved goal list.
        self.assertEqual(assistant.generate_conversation.call_count, 2)
        session = unlock(self.store)
        ask(identifiers[0], session)
        self.assertEqual(requests.inbox(self.store.privacy)["items"][0]["answer"], clarification)
        self.assertCountEqual(followups.previous("mac", session)[-1]["goal_choices"], [first["id"], second["id"]])
        self.assertEqual(self.goals.overview(), sorted([first, second, *unrelated], key=lambda goal: goal["id"]))
        asked = follow_up
        self.assertEqual(ask(identifiers[1], session)["goal_id"], first["id"])
        self.assertEqual(requests.inbox(self.store.privacy)["items"][0]["answer"], "I saved PDF output for Garden check.")
        updated = self.goals.detail(first["id"])
        self.assertEqual(updated["pdf_instruction"], self.instruction)
        for key in ("prompt", "title", "scheduled_at", "next_run", "frequency_minutes", "zone"):
            self.assertEqual(updated[key], first[key])
        self.assertEqual(self.goals.detail(second["id"])["revision"], second["revision"])
        self.assertEqual(self.goals.detail(unrelated[0]["id"])["revision"], unrelated[0]["revision"])

    def test_preserves_an_existing_reserved_check_in_and_retries_its_pdf_independently(self):
        goal = self.save("Seedling check", "Ask if I watered the seedlings.", check_in=True)
        identifier = goal["id"]
        question = "Did you water the seedlings?"
        choices = ["Yes", "Not yet"]
        jobs = GoalFollowUpJobs(self.store, lambda *_: json.dumps(
            {"name": "schedule_follow_up", "question": question, "choices": choices, "use_goal_run": True},
        ))
        jobs.decide(self.anchor)
        before = self.goals.detail(identifier)
        self.assertIsNotNone(before["follow_up"])
        self.goals.set_pdf_format(identifier, before["revision"], self.instruction)
        saved = self.goals.detail(identifier)
        self.assertEqual(saved["follow_up"], before["follow_up"])
        self.assertEqual(saved["next_run"], before["next_run"])
        due = datetime.fromisoformat(saved["follow_up"]["due"])
        jobs.deliver(due)
        card = self.goals.detail(identifier)["follow_up"]
        self.assertTrue(card["can_answer"])
        self.assertEqual(card["question"], question)
        with self.assertLogs("nex.goal_follow_up_pdfs", level="WARNING"):
            deliver_pdfs(self.store, lambda *_: None, due)

        def generate(messages, _tokens):
            self.assertEqual(messages[0]["content"], WRITE_DOCUMENT)
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["document_brief"], self.instruction)
            self.assertEqual(data["published_check_in"], [question, *choices])
            return json.dumps({"title": goal["title"], "layout": "report",
                               "sections": [{"paragraphs": [question, *choices]}]})

        deliver_pdfs(self.store, generate, due + timedelta(minutes=5))
        deliver_pdfs(self.store, generate, due + timedelta(minutes=10))
        item, = artifacts(self.store, identifier)
        self.assertEqual((item["id"], item["kind"]), (card["id"], "pdf"))
        message, = [msg for msg in chat_history(self.store) if msg["id"] == card["id"]]
        self.assertEqual(message["pdf_attachment"]["id"], card["id"])
        self.assertEqual(message["follow_up"]["choices"], choices)

    def test_existing_database_keeps_pending_run_when_pdf_format_is_saved(self):
        goal = self.save("Monthly archive", "Summarize the archived reports.")
        with self.store.connect() as db:
            db.execute("ALTER TABLE goals DROP COLUMN pdf_instruction")
            db.execute("DROP INDEX goal_follow_up_pdf_due")
            db.execute("ALTER TABLE goal_follow_ups DROP COLUMN pdf_retry_at")
            db.execute("ALTER TABLE goal_follow_ups DROP COLUMN pdf_instruction")
            db.execute("UPDATE goals SET requested=1,latest='Earlier report' WHERE id=?", (goal["id"],))
        store = Store(self.root, 0, 2**20)
        goals = Goals(store)
        goals.set_pdf_format(goal["id"], goal["revision"], self.instruction)
        with store.connect() as db:
            row = Goals.find(db, goal["id"])
        self.assertEqual((row["requested"], row["latest"], row["next_run"]), (1, "Earlier report", goal["next_run"]))
