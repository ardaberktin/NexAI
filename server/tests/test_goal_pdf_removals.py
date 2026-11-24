import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from conversation_fixtures import unlock
from nex.assistant import Assistant
from nex.conversation.document_routing import CHOOSE_DOCUMENT_ACTION
from nex.conversation.goal_management_prompts import (
    CHOOSE_DELETION_SCOPE,
    SELECT_PDF_REMOVAL_GOAL,
    VERIFY_PDF_REMOVAL,
)
from nex.conversation.goal_pdf_removal import EXTRACT_GOAL_REFERENCE, FIND_GOAL_SUBJECTS
from nex.conversation.reply import (
    VERIFY_GOAL_PDF_REMOVAL,
    VERIFY_UNSAVED_GOAL,
    WRITE_GOAL_CLARIFICATION,
    WRITE_GOAL_PDF_REMOVAL,
    WRITE_UNSAVED_GOAL,
)
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goal_follow_up_pdfs import deliver_pdfs
from nex.goal_runner import EXECUTE
from nex.goal_update_pdfs import deliver_update_pdfs
from nex.goals import Goals
from nex.privacy import public, requests
from nex.privacy.chat_history import ChatFollowups
from nex.scheduled_plan import PLAN
from nex.store import Store
from nex.workspace_content import artifacts, chat_history
from nex.workspace_schema import WorkspaceConflict


class GoalPDFRemovalTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())), 0, 2**20)
        self.goals = Goals(self.store)
        self.assistant = Assistant(self.store, None, None)
        self.followups = ChatFollowups()
        self.session = unlock(self.store)
        self.anchor = datetime.now(UTC).replace(second=0, microsecond=0)
        self.due = self.anchor + timedelta(hours=1)

    def save(self, title, *, check_in=False):
        data = {"title": title, "prompt": f"Give an update on {title}.", "zone": "UTC",
                "scheduled_at": self.due.isoformat(), "frequency_minutes": 0 if check_in else 1440}
        goal = self.goals.save(str(uuid4()), data, anchor=self.anchor, check_in=check_in)
        return self.goals.set_pdf_format(goal["id"], goal["revision"], "Attach a PDF to each update.")

    def ask(self, asked, generate, identifier=None, *, locked=False):
        self.assistant.generate_conversation = generate
        return public.chat(self.assistant, identifier or str(uuid4()), "mac", asked, "en", "UTC",
                           followups=self.followups, session=None if locked else self.session)

    def model(self, target, authorization=None, *, needs_choice=False, reject_change=False,
              route="DOCUMENT", answer=None, missing=False):
        answer = answer or target

        def generate(messages, *_args, **_options):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            if prompt.startswith(ROUTE_HEADER):
                return "MANAGE" if data["latest_message"] == answer else route
            if prompt == CHOOSE_DELETION_SCOPE:
                return "PDF"
            if prompt == CHOOSE_DOCUMENT_ACTION:
                if data["question"] == answer:
                    self.assertEqual(data["pending_pdf_action"], "REMOVE")
                return '{"action":"REMOVE","sources":[]}'
            if prompt == EXTRACT_GOAL_REFERENCE:
                return json.dumps({"reference": None})
            if prompt == FIND_GOAL_SUBJECTS:
                ambiguous = needs_choice and data["user_reference"] != answer
                return json.dumps({"evidence": "Compared the subjects.",
                                   "matches": [row["number"] for row in data["goals"]
                                               if not missing and (ambiguous or row["name"] == target)]})
            if prompt == SELECT_PDF_REMOVAL_GOAL:
                record, = [row for row in data["goals"] if row["title"] == target]
                return json.dumps({key: record[key] for key in ("target", "title")})
            if prompt == VERIFY_PDF_REMOVAL:
                self.assertEqual(data["selected_goal"], target)
                if authorization:
                    self.assertEqual(data["preceding_clarification"][0]["content"], authorization)
                    self.assertEqual(data["latest_message"], answer)
                return "YES"
            if prompt == WRITE_GOAL_CLARIFICATION:
                return json.dumps({"question": "Which goal do you mean, " + " or ".join(data["candidate_titles"]) + "?"})
            if prompt == WRITE_GOAL_PDF_REMOVAL:
                receipt = data["tools"]["goal"]
                self.assertEqual(receipt["title"], target)
                self.assertFalse(receipt["pdf_enabled"])
                return f"PDF copies are disabled for {target}."
            if prompt == VERIFY_GOAL_PDF_REMOVAL:
                self.assertEqual(data["goal"]["title"], target)
                return "YES"
            self.assertTrue(reject_change)
            if prompt.startswith(WRITE_UNSAVED_GOAL):
                status = data["outcome"]["status"]
                if status == "change_rejected":
                    return "I removed the PDF." if prompt == WRITE_UNSAVED_GOAL else "The goal changed before I could save."
                self.assertIn(status, {"unverified_change", "no_matching_goal"})
                return "No change was saved."
            self.assertEqual(prompt, VERIFY_UNSAVED_GOAL)
            self.assertFalse(data["outcome"]["changed"])
            return "NO" if data["draft"] == "I removed the PDF." else "YES"

        return generate

    def test_remove_pdf_requires_unlock_and_preserves_task_and_schedule(self):
        goal = self.save("Greenhouse briefing")
        question = "Stop including PDF copies in my Greenhouse briefing."
        model = self.model(goal["title"])
        identifier = str(uuid4())
        self.assertEqual(self.ask(question, model, identifier, locked=True)["engine"], "private_request")
        self.assertEqual(self.goals.overview(), [goal])
        self.assertEqual(self.ask(question, model, identifier)["goal_id"], goal["id"])
        updated, = self.goals.overview()
        expected = {key: value for key, value in goal.items() if key not in {"pdf_instruction", "revision"}}
        self.assertEqual(updated, expected | {"revision": goal["revision"] + 1})
        self.assertEqual(self.ask(question, model)["goal_id"], goal["id"])
        self.assertEqual(self.goals.overview(), [updated])
        self.assertIn("disabled", requests.inbox(self.store.privacy)["items"][0]["answer"])

    def test_ambiguous_removal_retains_choices_and_resolves_the_selected_goal(self):
        garden = self.save("Garden check")
        kitchen = self.save("Kitchen check")
        question = "Stop attaching PDFs to my check-ins."
        model = self.model(kitchen["title"], question, needs_choice=True)
        self.assertIsNone(self.ask(question, model).get("goal_id"))
        self.assertCountEqual(self.goals.overview(), [garden, kitchen])
        self.assertCountEqual(self.followups.previous("mac", self.session)[-1]["goal_choices"],
                              [garden["id"], kitchen["id"]])
        self.assertEqual(self.ask(kitchen["title"], model)["goal_id"], kitchen["id"])
        self.assertNotIn("pdf_instruction", self.goals.detail(kitchen["id"]))
        self.assertIn(garden, self.goals.overview())

    def test_short_subject_resolves_only_the_offered_goal_and_keeps_the_original_request(self):
        morning = self.save("Morning report")
        card = self.save("Ask whether the new Walmart card arrived")
        school = self.save("Ask whether the campus library card arrived")
        question = "Stop sending the PDF with my checks."
        answer = "walmart card"
        model = self.model(card["title"], question, needs_choice=True, answer=answer)
        self.assertIsNone(self.ask(question, model).get("goal_id"))
        offered = self.followups.previous("mac", self.session)[-1]["goal_choices"]
        self.assertCountEqual(offered, [morning["id"], card["id"], school["id"]])
        def narrow(messages, *args, **options):
            if messages[0]["content"] == FIND_GOAL_SUBJECTS:
                data = json.loads(messages[1]["content"])
                self.assertEqual(data["user_reference"], "the card")
                targets = [row["number"] for row in data["goals"] if row["name"] in (card["title"], school["title"])]
                return json.dumps({"evidence": "Two goals track cards.", "matches": targets})
            return model(messages, *args, **options)

        self.assertIsNone(self.ask("the card", narrow).get("goal_id"))
        self.assertCountEqual(self.goals.overview(), [morning, card, school])
        later = self.save("Walmart card statement")

        def resolve(messages, *args, **options):
            if messages[0]["content"] == FIND_GOAL_SUBJECTS:
                data = json.loads(messages[1]["content"])
                self.assertEqual(data["user_reference"], answer)
                self.assertEqual([entry["content"] for entry in data["previous_exchange"] if entry["role"] == "user"],
                                 [question, "the card"])
                self.assertCountEqual([row["name"] for row in data["goals"]], [card["title"], school["title"]])
            return model(messages, *args, **options)

        self.assertEqual(self.ask(answer, resolve)["goal_id"], card["id"])
        self.assertNotIn("pdf_instruction", self.goals.detail(card["id"]))
        self.assertEqual(self.goals.detail(card["id"])["next_run"], card["next_run"])
        for goal in (morning, school, later):
            self.assertIn(goal, self.goals.overview())

    def test_rechecks_scope_when_pdf_removal_was_routed_as_goal_deletion(self):
        goal = self.save("Warehouse briefing")
        result = self.ask("Remove the PDF from Warehouse briefing.", self.model(goal["title"], route="DELETE"))
        self.assertEqual(result["goal_id"], goal["id"])
        self.assertNotIn("pdf_instruction", self.goals.detail(goal["id"]))

    def test_model_selected_nickname_has_a_unique_grounded_reference(self):
        goal = self.save("Ask whether the library pass arrived")
        self.save("Garden check")
        demo = self.model(goal["title"])

        def generate(messages, *args, **options):
            if messages[0]["content"] == WRITE_GOAL_PDF_REMOVAL:
                return "The library pass check will send text updates only."
            return demo(messages, *args, **options)

        result = self.ask("Remove the PDF from my library pass check.", generate)
        self.assertEqual(result["goal_id"], goal["id"])
        self.assertIn("text updates only", requests.inbox(self.store.privacy)["items"][0]["answer"])
        self.assertNotIn("pdf_instruction", self.goals.detail(goal["id"]))

    def test_model_cannot_use_an_unrelated_reference_to_turn_off_a_saved_goal(self):
        garden = self.save("Garden check")
        kitchen = self.save("Kitchen check")
        demo = self.model(garden["title"], missing=True, reject_change=True)
        result = self.ask("Turn off PDF copies for my airport parking goal.", demo)
        self.assertIsNone(result.get("goal_id"))
        self.assertCountEqual(self.goals.overview(), [garden, kitchen])

    def test_verifier_can_reject_a_valid_target_that_belongs_to_the_wrong_goal(self):
        intended = self.save("Morning report")
        unrelated = self.save("Kitchen check")
        model = self.model(unrelated["title"], reject_change=True)
        asked = "Turn off PDF copies for the Morning report, please."

        def generate(messages, *args, **options):
            if messages[0]["content"] == SELECT_PDF_REMOVAL_GOAL:
                data = json.loads(messages[1]["content"])
                self.assertEqual(data["latest_message"], asked)
                record, = [row for row in data["goals"] if row["title"] == intended["title"]]
                return json.dumps({key: record[key] for key in ("target", "title")})
            return model(messages, *args, **options)

        self.assertIsNone(self.ask(asked, generate).get("goal_id"))
        self.assertCountEqual(self.goals.overview(), [intended, unrelated])

    def test_mismatched_goal_number_and_title_are_repaired_before_saving(self):
        intended = self.save("Pass renewal")
        other = self.save("Morning report")
        model = self.model(intended["title"])
        selections = []

        def generate(messages, *args, **options):
            if messages[0]["content"].startswith(SELECT_PDF_REMOVAL_GOAL):
                selections.append(messages)
                data = json.loads(messages[1]["content"])
                record, = [row for row in data["goals"] if row["title"] == intended["title"]]
                return json.dumps({"target": record["target"],
                                   "title": other["title"] if len(selections) == 1 else intended["title"]})
            return model(messages, *args, **options)

        self.assertEqual(self.ask("Turn off PDFs for pass renewal.", generate)["goal_id"], intended["id"])
        self.assertEqual(len(selections), 2)
        self.assertIn(other, self.goals.overview())

    def test_invalid_goal_reference_or_missing_verifier_returns_failed_status(self):
        goal = self.save("Library pass arrival")
        model = self.model(goal["title"])
        for stage, invalid in ((EXTRACT_GOAL_REFERENCE, None),
                               (FIND_GOAL_SUBJECTS, '{"evidence":"one goal","matches":[true]}'),
                               (FIND_GOAL_SUBJECTS, '{"evidence":"unknown reference","matches":[99]}'),
                               (SELECT_PDF_REMOVAL_GOAL, None), (VERIFY_PDF_REMOVAL, None)):
            with self.subTest(stage=stage, invalid=invalid):
                def generate(messages, *args, stage=stage, invalid=invalid, **options):
                    if messages[0]["content"] == stage:
                        return invalid
                    return model(messages, *args, **options)

                result = self.ask("Turn off PDF copies for the library pass.", generate)
                self.assertFalse(result.get("goal_id"))
                row = requests.inbox(self.store.privacy)["items"][0]
                self.assertEqual(row["state"], "failed")
                self.assertFalse(row["answer"])
                self.assertEqual(self.goals.overview(), [goal])

    def test_rejected_removal_cannot_confirm_a_change(self):
        goal = self.save("Warehouse briefing")
        question = "Send my Warehouse briefing without PDF copies."
        with patch.object(Goals, "set_pdf_format", side_effect=WorkspaceConflict("The goal revision changed.")):
            result = self.ask(question, self.model(goal["title"], reject_change=True))
        self.assertIsNone(result.get("goal_id"))
        self.assertEqual(requests.inbox(self.store.privacy)["items"][0]["answer"],
                         "The goal changed before I could save.")
        self.assertEqual(self.goals.overview(), [goal])

    def test_disabling_cancels_inflight_copy_but_preserves_existing_pdf_and_future_text(self):
        goal = self.save("Warehouse briefing")

        def generate(messages, _tokens):
            prompt = messages[0]["content"]
            if prompt == PLAN:
                return '{"sources":["none"]}'
            self.assertEqual(prompt, EXECUTE)
            return "The stocktake is complete."

        runner = Goals(self.store, generate=generate)
        layout = '{"title":"Warehouse briefing","layout":"report"}'
        runner.run(self.due)
        deliver_update_pdfs(self.store, lambda *_: layout, self.due)
        existing, = artifacts(self.store, goal["id"])

        def clear_during_render(*_):
            self.goals.set_pdf_format(goal["id"], goal["revision"], None)
            return layout

        second_due = self.due + timedelta(days=1)
        runner.run(second_due)
        deliver_update_pdfs(self.store, clear_during_render, second_due)
        self.assertEqual([item for item in artifacts(self.store, goal["id"]) if item["kind"] == "pdf"], [existing])
        runner.run(second_due + timedelta(days=1))
        forbid_render = Mock(side_effect=AssertionError("PDF rendering should stay disabled"))
        deliver_update_pdfs(self.store, forbid_render, second_due + timedelta(days=1))
        forbid_render.assert_not_called()
        updates = [row for row in chat_history(self.store) if row.get("goal_id") == goal["id"]]
        self.assertEqual(len(updates), 3)
        self.assertTrue(all(row["text"] == "The stocktake is complete." for row in updates))
        self.assertEqual([row["id"] for row in updates if row.get("pdf_attachment")], [existing["id"]])

    def test_disabling_cancels_pending_follow_up_copy_and_leaves_question_answerable(self):
        goal = self.save("Seedling check", check_in=True)
        question = "Did you water the seedlings?"
        jobs = GoalFollowUpJobs(self.store, lambda *_: json.dumps(
            {"name": "schedule_follow_up", "question": question, "choices": ["Yes", "Not yet"], "use_goal_run": True},
        ))
        jobs.decide(self.anchor)
        follow_up = self.goals.detail(goal["id"])["follow_up"]
        due = datetime.fromisoformat(follow_up["due"])
        jobs.deliver(due)
        published = self.goals.detail(goal["id"])["follow_up"]
        self.assertTrue(published["can_answer"])
        self.goals.set_pdf_format(goal["id"], goal["revision"], None)
        forbid_render = Mock(side_effect=AssertionError("Follow-up PDF was disabled"))
        deliver_pdfs(self.store, forbid_render, due)
        forbid_render.assert_not_called()
        self.assertEqual(self.goals.detail(goal["id"])["follow_up"], published)
        self.assertTrue(all(item["kind"] != "pdf" for item in artifacts(self.store, goal["id"])))
