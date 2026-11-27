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
    RESOLVE_PDF_REMOVAL,
)
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


def removal_decision(goals, titles):
    choices = {label: label.partition(". ")[2] in titles for label in goals}
    return json.dumps({"action": "disable_future_pdfs", "subject": "the named task", "goals": choices})


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
            if prompt == RESOLVE_PDF_REMOVAL:
                latest = messages[-1]["content"] if len(messages) > 2 else data["latest_message"]
                ambiguous = needs_choice and latest != answer
                if authorization and not ambiguous:
                    self.assertEqual(data["request"], authorization)
                    self.assertEqual(messages[-1]["content"], answer)
                goals = data["goals"]
                titles = [label.partition(". ")[2] for label in goals] if ambiguous else [target]
                return removal_decision(goals, [] if missing else titles)
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
                self.assertIn(status, {"no_matching_goal", "no_change_requested"})
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
            if messages[0]["content"] == RESOLVE_PDF_REMOVAL:
                data = json.loads(messages[1]["content"])
                self.assertEqual(messages[-1]["content"], "the card")
                goals = data["goals"]
                return removal_decision(goals, [card["title"], school["title"]])
            return model(messages, *args, **options)

        self.assertIsNone(self.ask("the card", narrow).get("goal_id"))
        self.assertCountEqual(self.goals.overview(), [morning, card, school])
        later = self.save("Walmart card statement")

        def resolve(messages, *args, **options):
            if messages[0]["content"] == RESOLVE_PDF_REMOVAL:
                data = json.loads(messages[1]["content"])
                self.assertEqual(messages[-1]["content"], answer)
                self.assertEqual(data["request"], question)
                self.assertEqual([entry["content"] for entry in messages[2:-1] if entry["role"] == "user"],
                                 ["the card"])
                self.assertCountEqual([label.partition(". ")[2] for label in data["goals"]], [card["title"], school["title"]])
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

    def test_generic_confirmation_is_rewritten_with_the_saved_goal_title(self):
        goal = self.save("Ask whether the library pass arrived")
        self.save("Garden check")
        demo = self.model(goal["title"])
        drafts = []

        def generate(messages, *args, **options):
            if messages[0]["content"].startswith(WRITE_GOAL_PDF_REMOVAL):
                drafts.append(messages[0]["content"])
                if len(drafts) == 1:
                    return "The goal’s updates have PDF attachments turned off."
                return f'PDF copies for “{goal["title"]}” are off.'
            return demo(messages, *args, **options)

        result = self.ask("Remove the PDF from my library pass check.", generate)
        self.assertEqual(result["goal_id"], goal["id"])
        self.assertEqual(len(drafts), 2)
        self.assertIn(goal["title"], requests.inbox(self.store.privacy)["items"][0]["answer"])
        self.assertNotIn("pdf_instruction", self.goals.detail(goal["id"]))

    def test_failed_confirmation_preserves_the_committed_goal_without_publishing_a_generic_reply(self):
        goal = self.save("Rent")
        demo = self.model(goal["title"])

        def generate(messages, *args, **options):
            if messages[0]["content"].startswith(WRITE_GOAL_PDF_REMOVAL):
                # "currently" contains the title as a substring.
                return "PDF attachments are currently disabled for the goal."
            return demo(messages, *args, **options)

        self.ask("Turn off the PDFs for Rent.", generate)
        row, = requests.inbox(self.store.privacy)["items"]
        self.assertEqual((row["state"], row["goal_id"], row["answer"]), ("failed", goal["id"], None))
        self.assertEqual(self.goals.detail(goal["id"])["revision"], goal["revision"] + 1)
        self.assertNotIn("pdf_instruction", self.goals.detail(goal["id"]))

    def test_model_cannot_use_an_unrelated_reference_to_turn_off_a_saved_goal(self):
        garden = self.save("Garden check")
        kitchen = self.save("Kitchen check")
        demo = self.model(garden["title"], missing=True, reject_change=True)
        result = self.ask("Turn off PDF copies for my airport parking goal.", demo)
        self.assertIsNone(result.get("goal_id"))
        self.assertCountEqual(self.goals.overview(), [garden, kitchen])

    def test_cancelled_removal_decision_is_honored_even_if_routed_to_the_tool(self):
        goal = self.save("Morning report")
        model = self.model(goal["title"], reject_change=True)
        question = "Actually, leave the PDF on for the Morning report."

        def generate(messages, *args, **options):
            if messages[0]["content"] == RESOLVE_PDF_REMOVAL:
                self.assertEqual(json.loads(messages[1]["content"])["latest_message"], question)
                return json.dumps({"action": "keep_settings", "reason": "leave the PDF on"})
            return model(messages, *args, **options)

        self.assertIsNone(self.ask(question, generate).get("goal_id"))
        self.assertEqual(self.goals.overview(), [goal])

    def test_cancelling_a_pending_removal_stays_private_and_does_not_replan_or_edit(self):
        goal = self.save("Pass renewal")
        self.followups.save("mac", self.session, (), "prior", "Switch the checks to text only.", "Which check?",
                            private=True, selected_tools=["manage_goal"],
                            selected='[{"name":"manage_goal","operation":"remove_pdf"}]', goal_choices=[goal["id"]])
        demo = self.model(goal["title"], reject_change=True)
        routes = []

        def generate(messages, *args, **options):
            prompt = messages[0]["content"]
            if prompt.startswith(ROUTE_HEADER):
                routes.append(messages)
                return '{"capabilities":["document"],"language":"en"}'
            if prompt == CHOOSE_DOCUMENT_ACTION:
                self.assertEqual(json.loads(messages[1]["content"])["pending_pdf_action"], "REMOVE")
                return '{"action":"SKIP","sources":[]}'
            self.assertNotEqual(prompt, RESOLVE_PDF_REMOVAL)
            return demo(messages, *args, **options)

        result = self.ask("Actually, leave the PDF on.", generate)
        self.assertEqual(result["engine"], "private_request")
        self.assertEqual(len(routes), 1)
        row, = requests.inbox(self.store.privacy)["items"]
        self.assertEqual((row["state"], row["answer"]), ("ready", "No change was saved."))
        self.assertEqual(self.goals.overview(), [goal])

    def test_mismatched_goal_number_and_title_are_repaired_before_saving(self):
        intended = self.save("Pass renewal")
        other = self.save("Morning report")
        model = self.model(intended["title"])
        selections = []

        def generate(messages, *args, **options):
            if messages[0]["content"].startswith(RESOLVE_PDF_REMOVAL):
                selections.append(messages)
                data = json.loads(messages[1]["content"])
                decision = json.loads(removal_decision(data["goals"], [intended["title"]]))
                if len(selections) == 1:
                    choices = decision["goals"]
                    label = next(key for key, matched in choices.items() if matched)
                    choices[label.partition(". ")[0] + ". " + other["title"]] = choices.pop(label)
                return json.dumps(decision)
            return model(messages, *args, **options)

        self.assertEqual(self.ask("Turn off PDFs for pass renewal.", generate)["goal_id"], intended["id"])
        self.assertEqual(len(selections), 2)
        self.assertIn(other, self.goals.overview())

    def test_invalid_or_missing_decision_returns_failed_status_without_saving(self):
        goal = self.save("Library pass arrival")
        model = self.model(goal["title"])
        key = f'1. {goal["title"]}'
        invalid_outputs = [None, "{}", '{"action":"keep_settings","goals":{}}']
        invalid_outputs.extend(json.dumps({"action": "disable_future_pdfs", "subject": "the pass", "goals": fields})
                               for fields in ({}, {"99. Other goal": True}, {key: 1}, {key: True, "2. Other": False}))
        for invalid in invalid_outputs:
            with self.subTest(invalid=invalid):
                def generate(messages, *args, invalid=invalid, **options):
                    if messages[0]["content"].startswith(RESOLVE_PDF_REMOVAL):
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
