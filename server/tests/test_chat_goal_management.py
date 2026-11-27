import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conversation_fixtures import unlock
from nex.assistant import Assistant
from nex.conversation.goal_edit import PDF_MODE
from nex.conversation.goal_edit_fields import CHOOSE_FIELDS
from nex.conversation.goal_edit_receipt import VERIFY_EDIT, WRITE_EDIT
from nex.conversation.goal_management_prompts import (
    CHOOSE_DELETION_SCOPE,
    EDIT_GOAL,
    FIND_GOALS,
    MANAGE_GOALS,
    SELECT_GOAL,
    VERIFY_CHANGE,
    VERIFY_NAMED_DELETE,
)
from nex.conversation.reply import (
    VERIFY_GOAL,
    VERIFY_GOAL_FORMAT,
    VERIFY_GOAL_PDF_REMOVAL,
    VERIFY_GOAL_STATUS,
    VERIFY_UNSAVED_GOAL,
    WRITE_GOAL,
    WRITE_GOAL_CLARIFICATION,
    WRITE_GOAL_FORMAT,
    WRITE_GOAL_PDF_REMOVAL,
    WRITE_GOAL_STATUS,
    WRITE_UNSAVED_GOAL,
)
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goals import Goals
from nex.privacy import public, requests
from nex.privacy.chat_history import ChatFollowups
from nex.store import Store


class ChatGoalManagementTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self.enterContext(tempfile.TemporaryDirectory()), 0, 2**20)
        self.assistant = Assistant(self.store, None, None)
        self.assistant.generate_conversation = self.generate
        self.goals = Goals(self.store)
        self.card = self.save("Ask whether the replacement library pass arrived.", check_in=True)
        self.other = self.save("Pick up the dry cleaning")
        self.decision = "complete"
        self.edit = {}
        self.verify = lambda: "MATCH"
        self.reads = 0
        self.intent = "MANAGE"
        self.use_history = False
        self.match_all = False
        self.deletion_review = None
        self.verify_receipt = True

    def save(self, task, **options):
        return self.goals.save(str(uuid4()), {"prompt": task, "zone": "UTC"}, **options)

    def generate(self, messages, *args, **kwargs):
        prompt = messages[0]["content"]
        data = json.loads(messages[1]["content"])
        if prompt.startswith(ROUTE_HEADER):
            self.assertNotIn("goals", data)
            return self.intent
        if prompt == CHOOSE_DELETION_SCOPE:
            return "GOAL"
        if prompt == FIND_GOALS:
            return json.dumps({"matches": [self.match_all or self.card["title"] in label for label in data["goals"]]})
        if prompt.startswith((MANAGE_GOALS, SELECT_GOAL)):
            self.reads += 1
            if self.use_history and "history" not in data:
                return '{"action":"use_history"}'
            if isinstance(self.decision, dict):
                return json.dumps(self.decision)
            selected, = [row for row in data["goals"] if row["title"] == self.card["title"]]
            return json.dumps({"action": self.decision, "target": selected["target"], "title": selected["title"]})
        if prompt == PDF_MODE:
            return json.dumps({"setting": self.pdf_mode})
        if prompt == CHOOSE_FIELDS:
            return json.dumps({"fields": [key for key in self.edit if key not in {"question", "timing"}]})
        if prompt.startswith(EDIT_GOAL):
            return json.dumps(self.edit)
        if prompt.startswith(VERIFY_CHANGE):
            return self.verify()
        if prompt == VERIFY_NAMED_DELETE:
            self.deletion_review = data
            return "YES" if self.verify() == "MATCH" else "NO"
        if prompt in (WRITE_GOAL_FORMAT, WRITE_GOAL_PDF_REMOVAL):
            return f"Updated PDF copies for {data['tools']['goal']['title']}."
        if prompt in (VERIFY_GOAL_FORMAT, VERIFY_GOAL_PDF_REMOVAL):
            return "YES"
        if prompt.startswith((WRITE_GOAL, WRITE_GOAL_STATUS, WRITE_EDIT)):
            receipt = data["tools"]["goal"]
            text = f"Goal {receipt['action']}: {receipt['title']}."
            schedule = receipt.get("schedule_text")
            return f"{text} Next check is {schedule}." if schedule else text
        if prompt in (VERIFY_GOAL, VERIFY_GOAL_STATUS, VERIFY_EDIT):
            return "YES" if self.verify_receipt else "NO"
        if prompt == WRITE_GOAL_CLARIFICATION:
            return json.dumps({"question": "Which goal do you mean, " + " or ".join(data["candidate_titles"]) + "?"})
        if prompt == VERIFY_UNSAVED_GOAL:
            return "YES"
        if prompt.startswith(WRITE_UNSAVED_GOAL):
            outcome = data["outcome"]
            self.assertFalse(outcome["changed"])
            if question := outcome.get("suggested_question"):
                return question
            if titles := outcome.get("candidate_titles"):
                return "Which goal should I remove? " + ", ".join(titles)
            if detail := outcome.get("detail"):
                return detail
            return "Which goal and change did you mean?"
        self.fail(prompt)

    def ask(self, text, identifier=None, **options):
        return public.chat(self.assistant, identifier or str(uuid4()), "mac", text, "en", "UTC", **options)

    def receipt(self):
        return requests.inbox(self.store.privacy)["items"][0]

    def schedule_check_in(self):
        def schedule(messages, _):
            data = json.loads(messages[1]["content"])
            if data["goal"] != self.card["prompt"]:
                return '{"name":"leave_as_is"}'
            if "question" in data:
                return '{"days_from_now":4,"clock":"17:00"}'
            return '{"name":"schedule_follow_up","question":"Did the pass arrive?","choices":["Yes","Not yet"]}'

        GoalFollowUpJobs(self.store, schedule).decide()
        return self.goals.detail(self.card["id"])["follow_up"]

    def test_generic_edit_adds_and_removes_the_actual_pdf_setting(self):
        session = unlock(self.store)
        self.intent = "EDIT"
        self.decision = "edit"
        for mode, request in (("enable_pdf", "Add a PDF copy to the library pass updates"),
                              ("remove_pdf", "Switch the library pass updates back to text only")):
            with self.subTest(mode=mode):
                self.pdf_mode = mode
                self.edit = {"format_instruction": request}
                self.ask(request, session=session)
                saved = self.goals.detail(self.card["id"])
                self.assertEqual(saved.get("pdf_instruction"), request if mode == "enable_pdf" else None)
                self.assertEqual(saved["prompt"], self.card["prompt"])
                self.assertEqual(self.receipt()["goal_id"], self.card["id"])

    def test_pause_inspect_and_resume_keep_the_definition_and_sibling(self):
        session = unlock(self.store)
        before = self.goals.detail(self.card["id"])
        for action, status, revision in (("pause", "paused", 1), ("inspect", "paused", 1), ("resume", "active", 2)):
            with self.subTest(action=action):
                self.intent = json.dumps({"capabilities": [action], "language": "en"})
                self.decision = action
                self.ask(f"{action} the replacement library pass goal", session=session)
                saved = self.goals.detail(self.card["id"])
                self.assertEqual((saved["status"], saved["revision"]), (status, before["revision"] + revision))
                self.assertEqual(saved["prompt"], before["prompt"])
                self.assertEqual(self.receipt()["goal_id"], saved["id"])
                self.assertEqual(self.goals.detail(self.other["id"])["revision"], self.other["revision"])

    def test_private_goal_directory_only_enters_the_unlocked_selector(self):
        from unittest.mock import patch

        from nex.conversation.routing_prompt import SAVED_ACTION

        seen = []
        def capture(messages, *args, **kwargs):
            seen.append((messages[0]["content"], json.loads(messages[1]["content"])))
            return self.generate(messages, *args, **kwargs)

        with patch.object(self.assistant, "generate_conversation", capture):
            self.ask("Has my library pass arrived?")
            self.assertFalse(any("saved_goal_titles" in data for _, data in seen))
            self.ask("Has my library pass arrived?", session=unlock(self.store))
        directories = [(prompt, data["saved_goal_titles"]) for prompt, data in seen if "saved_goal_titles" in data]
        self.assertEqual(len(directories), 1)
        self.assertEqual(directories[0][0], SAVED_ACTION)
        self.assertCountEqual(directories[0][1], [self.card["title"], self.other["title"]])

    def test_cold_chat_completes_only_the_matched_goal_after_unlock_and_cancels_its_check(self):
        self.assertIsNotNone(self.schedule_check_in())
        for index in range(12):
            self.save(f"Read chapter {index}")
        identifier = str(uuid4())
        text = "My new library pass came in!"
        self.ask(text, identifier)
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.goals.detail(self.card["id"])["status"], "active")
        session = unlock(self.store)
        self.ask(text, identifier, session=session)
        self.ask(text, identifier, session=session)
        saved = self.goals.detail(self.card["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertIsNone(saved["next_run"])
        self.assertIsNone(saved["follow_up"])
        self.assertEqual([a["title"] for a in saved["activity"]].count("Goal completed"), 1)
        self.assertIn("completed", self.receipt()["answer"])
        self.assertEqual(self.receipt()["goal_id"], self.card["id"])
        self.assertEqual(self.goals.detail(self.other["id"])["status"], "active")

    def test_rejected_confirmation_keeps_the_committed_goal_and_returns_status(self):
        self.verify_receipt = False
        identifier = str(uuid4())
        session = unlock(self.store)
        for _ in range(2):
            notice = self.ask("My new library pass just arrived.", identifier, session=session)
            self.assertEqual((notice["engine"], notice["text"], notice["goal_id"]),
                             ("private_request", "", self.card["id"]))
        receipt = self.receipt()
        self.assertEqual((receipt["state"], receipt["answer"], receipt["goal_id"]),
                         ("failed", None, self.card["id"]))
        saved = self.goals.detail(self.card["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual([a["title"] for a in saved["activity"]].count("Goal completed"), 1)

    def test_edits_date_task_and_name_on_the_same_goal_then_deletes_it_without_a_dead_link(self):
        session = unlock(self.store)
        self.decision = "edit"
        start = (datetime.now(UTC) + timedelta(days=6)).replace(hour=14, minute=0)
        date = start.strftime("%Y-%m-%d")
        self.edit = {"start": f"{date}T14:00", "timing": f"{date} at 2 pm"}
        self.ask(f"Move the pass check to {date} at 2 pm", session=session)
        saved = self.goals.detail(self.card["id"])
        self.assertEqual(datetime.fromisoformat(saved["scheduled_at"]).strftime("%Y-%m-%dT%H:%M"), self.edit["start"])
        self.assertEqual(saved["prompt"], self.card["prompt"])
        self.edit = {"query": "Ask whether the new pass works.", "title": "Test the pass"}
        self.ask("Rename that to Test the pass and ask whether it works.", session=session)
        self.card = self.goals.detail(self.card["id"])
        self.assertEqual((self.card["title"], self.card["prompt"], self.card["next_run"]),
                         (self.edit["title"], self.edit["query"], saved["next_run"]))
        self.decision = "delete"
        self.ask("Remove the pass goal.", session=session)
        self.assertEqual([row["id"] for row in self.goals.overview()], [self.other["id"]])
        self.assertIsNone(self.receipt()["goal_id"])
        self.assertIn("deleted", self.receipt()["answer"])

    def test_ambiguous_missing_mismatched_and_unverified_actions_cannot_mutate_goals(self):
        session = unlock(self.store)
        for choice in ({"action": "clarify", "question": "Which pass arrived?"}, {"action": "none"},
                       {"action": "delete", "target": 1, "title": "A different goal"}, "complete"):
            self.verify = lambda: "NO"
            self.decision = choice
            self.ask("The pass might get here tomorrow.", session=session)
            self.assertEqual(self.goals.detail(self.card["id"])["revision"], self.card["revision"])
            self.assertEqual(len(self.goals.overview()), 2)

    def test_concurrent_edit_invalidates_a_pending_delete(self):
        def rename_before_commit():
            self.goals.save(self.card["id"], self.card | {"title": "Replacement pass"})
            return "MATCH"

        self.decision = "delete"
        self.verify = rename_before_commit
        self.ask("Delete the library pass reminder.", session=unlock(self.store))
        self.assertEqual(self.goals.detail(self.card["id"])["title"], "Replacement pass")
        self.assertIn("changed", self.receipt()["answer"])

    def test_backend_rejects_action_and_title_mismatches_before_commit(self):
        self.intent = "DELETE"
        self.ask("Remove the library pass reminder.", session=unlock(self.store))
        self.assertEqual(self.goals.detail(self.card["id"])["status"], "active")
        self.assertIn("Which goal", self.receipt()["answer"])

    def test_named_deletion_cannot_be_redirected_to_another_saved_goal(self):
        self.intent = "DELETE"
        self.decision = {"action": "delete", "target": 2, "title": self.card["title"]}
        self.ask(f"Remove the goal named {self.other['title']}.", session=unlock(self.store))
        self.assertEqual({g["id"] for g in self.goals.overview()}, {self.card["id"], self.other["id"]})

    def test_removal_with_competing_matches_names_the_choices_before_any_write(self):
        self.intent = "DELETE"
        self.match_all = True
        options = {"session": unlock(self.store), "followups": ChatFollowups()}
        self.ask("Remove my reminder.", **options)
        self.assertEqual(self.reads, 0)
        self.assertIn(self.card["title"], self.receipt()["answer"])
        self.assertIn(self.other["title"], self.receipt()["answer"])
        self.assertEqual(len(self.goals.overview()), 2)
        self.decision = "delete"
        self.ask(self.card["title"], **options)
        self.assertEqual([g["id"] for g in self.goals.overview()], [self.other["id"]])
        self.assertEqual(self.deletion_review["preceding_clarification"][0]["content"], "Remove my reminder.")

    def test_rescheduling_can_ask_for_a_missing_date_and_use_the_answer(self):
        options = {"session": unlock(self.store), "followups": ChatFollowups()}
        self.intent = "EDIT"
        self.decision = "edit"
        self.edit = {"question": "When should I ask about the library pass?"}
        self.ask("Change the date of the library pass reminder.", **options)
        self.assertEqual(self.receipt()["answer"], self.edit["question"])
        self.assertEqual(self.goals.detail(self.card["id"])["revision"], self.card["revision"])
        self.use_history = True
        day = (datetime.now(UTC) + timedelta(days=4)).date().isoformat()
        self.edit = {"start": f"{day}T13:00", "timing": f"{day} at 1 pm"}
        self.ask(f"{day} at 1 pm, please.", **options)
        self.assertEqual(self.receipt()["goal_id"], self.card["id"])
        self.assertTrue(self.goals.detail(self.card["id"])["next_run"].startswith(self.edit["start"]))

    def test_rename_preserves_an_inferred_check_in_and_task_edit_keeps_its_time(self):
        follow_up = self.schedule_check_in()
        self.intent = "EDIT"
        self.decision = "edit"
        session = unlock(self.store)
        self.edit = {"title": "Library follow-up"}
        self.ask("Rename the library goal to Library follow-up", session=session)
        self.card = self.goals.detail(self.card["id"])
        self.assertEqual(self.card["title"], "Library follow-up")
        self.assertEqual(self.card["follow_up"], follow_up)
        self.edit = {"query": "Ask whether I have activated the library pass."}
        self.ask("Change the question to whether I've activated the pass.", session=session)
        saved = self.goals.detail(self.card["id"])
        self.assertEqual(saved["next_run"], follow_up["due"])
        self.assertEqual(saved["title"], self.card["title"])
