import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from pypdfium2 import PdfDocument

from conversation_fixtures import tools, unlock
from nex.assistant import Assistant
from nex.conversation.catalog import VERIFY_GOAL_PLAN
from nex.conversation.document_routing import (
    CHOOSE_DOCUMENT_ACTION,
    PLAN_PDF,
    PLAN_PDF_GOAL,
)
from nex.conversation.goal_management_prompts import (
    EDIT_GOAL,
    GOAL_INTENT,
    SELECT_GOAL,
    VERIFY_CHANGE,
)
from nex.conversation.reply import (
    VERIFY_GOAL,
    VERIFY_GOAL_FORMAT,
    WRITE,
    WRITE_GOAL,
    WRITE_GOAL_FORMAT,
)
from nex.goal_runner import EXECUTE
from nex.goals import Goals
from nex.pdf.artifacts import content
from nex.pdf.document import WRITE_DOCUMENT
from nex.pdf.scheduled import VERIFY_SKIP_PDF, WRITE_NOTE
from nex.pdf.web import VERIFY_DOCUMENT_WEB, WRITE_WEB_DOCUMENT
from nex.privacy import public
from nex.scheduled_plan import PLAN, RETRY_PLAN
from nex.store import Store
from nex.web_html import Page
from nex.web_transport import WebReadError
from nex.workspace_content import artifacts

DATE = "2026-11-23"
TITLE = "Library opens new reading room"
STORY = "The library opened its new reading room."
URL = "https://example.test/releases/2026/1123"
PAGE = Page(TITLE, URL, f"City Library, {DATE}. {STORY}")
EDITION = {
    "title": "Today's Edition",
    "subtitle": DATE,
    "layout": "newspaper",
    "sections": [{"paragraphs": [STORY + " [1]"]}],
}


class PDFRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self.enterContext(tempfile.TemporaryDirectory()), 0, 2**20)
        self.assistant = Assistant(self.store, None, None)
        self.session = unlock(self.store)

    def ask(self, question, generate):
        self.assistant.generate_conversation = generate
        return public.chat(self.assistant, str(uuid4()), "mac", question, "en", "UTC", session=self.session)

    def test_immediate_news_pdf_recovers_from_agenda_only_plan(self):
        for question in ("Could you make a PDF of today's news?", "Put the day's top headlines in a newspaper PDF."):
            with self.subTest(question=question):
                def generate(messages, *_args, question=question, **_options):
                    prompt = messages[0]["content"]
                    data = json.loads(messages[1]["content"])
                    if prompt == GOAL_INTENT:
                        self.assertEqual(data["latest_message"], question)
                        return "DOCUMENT"
                    self.assertEqual(data["question"], question)
                    if prompt == CHOOSE_DOCUMENT_ACTION:
                        return "NOW"
                    if prompt.startswith(PLAN_PDF):
                        if len(messages) == 2:
                            return tools({"name": "agenda", "query": "today"})
                        self.assertIn("Include create_pdf", messages[-1]["content"])
                        return tools({"name": "create_pdf"},
                                     {"name": "web", "date": DATE, "query": "invented public topic"})
                    if prompt.startswith(WRITE_WEB_DOCUMENT):
                        self.assertTrue(data["cite_web_with_numbers"])
                        self.assertEqual(data["evidence"]["web"][0]["url"], URL)
                        if "revision_needed" not in data:
                            return json.dumps(EDITION | {"sections": [{"paragraphs": ["It opened at 10:30 a.m. [1]"]}]})
                        self.assertIn("not stated in the cited source", data["revision_needed"])
                        return json.dumps(EDITION | {"sections": [{"paragraphs": [
                            STORY + " [1]", "It opened at 10:30 a.m. [1]",
                        ]}]})
                    if prompt == VERIFY_DOCUMENT_WEB:
                        self.assertEqual(data["cited_pages"][0]["excerpt"], PAGE.text)
                        self.assertNotIn("10:30", data["document"])
                        return "YES"
                    self.assertTrue(prompt.startswith(WRITE))
                    self.assertEqual(data["tools"]["create_pdf"]["status"], "saved")
                    return "Today's Edition is in Files."

                before = {item["id"] for item in artifacts(self.store)}
                with patch("nex.conversation.tools.news_pages", return_value=[PAGE]) as lookup:
                    self.ask(question, generate)
                self.assertEqual(lookup.call_args.args[0].isoformat(), DATE)
                item, = [item for item in artifacts(self.store) if item["id"] not in before]
                with PdfDocument(content(self.store, item["id"])) as pdf:
                    text = pdf[0].get_textpage().get_text_bounded()
                    self.assertIn("reading room", text)
                    self.assertIn(URL, text)

    def test_missing_web_evidence_cannot_publish_a_pdf(self):
        def generate(messages, *_args, **_options):
            prompt = messages[0]["content"]
            if prompt == GOAL_INTENT:
                return "DOCUMENT"
            if prompt == CHOOSE_DOCUMENT_ACTION:
                return "NOW"
            if prompt.startswith(PLAN_PDF):
                return tools({"name": "web", "query": "public museum opening"}, {"name": "create_pdf"})
            self.assertTrue(prompt.startswith(WRITE))
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["tools"]["create_pdf"]["status"], "web_unavailable")
            return "The search didn't return sources for the PDF."

        with patch("nex.conversation.tools.prepare_lookup", side_effect=WebReadError("Offline")) as lookup:
            self.ask("Make a PDF about the public museum opening.", generate)
            lookup.assert_called_once()
            lookup.reset_mock()
            self.ask("Make a PDF about the public museum opening and my calendar.", generate)
            lookup.assert_not_called()
        self.assertEqual(artifacts(self.store), [])

    def test_recurring_delivery_requires_real_schedule_fields(self):
        first = (datetime.now(UTC) + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        question = "Starting tomorrow, give me the workshop bulletin as a PDF every morning at 8."
        task = "Publish the workshop bulletin as a PDF."

        def generate(messages, *_args, **_options):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            if prompt == GOAL_INTENT:
                self.assertEqual(data["latest_message"], question)
                return "DOCUMENT"
            if prompt == CHOOSE_DOCUMENT_ACTION:
                return "REPEAT"
            if prompt.startswith(PLAN_PDF_GOAL):
                if len(messages) == 2:
                    return tools({"name": "goal", "query": task + " Every morning at 8."})
                self.assertIn("supply frequency in minutes", messages[-1]["content"])
                return tools({"name": "goal", "query": task, "frequency": 1440,
                              "start": first.strftime("%Y-%m-%dT%H:%M")})
            if prompt in {VERIFY_GOAL, VERIFY_GOAL_PLAN}:
                return "YES"
            self.assertEqual(prompt, WRITE_GOAL)
            return "Saved for " + data["tools"]["goal"]["schedule_text"] + "."

        answer = self.ask(question, generate)
        goal = Goals(self.store).detail(answer["goal_id"])
        self.assertEqual(goal["prompt"], task)
        self.assertEqual(goal["frequency_minutes"], 1440)
        self.assertEqual(datetime.fromisoformat(goal["next_run"]), first)
        self.assertEqual(artifacts(self.store), [])

    def test_recurring_goal_format_edit_reaches_each_run_and_preserves_the_schedule(self):
        anchor = (datetime.now(UTC) + timedelta(hours=1)).replace(second=0, microsecond=0)
        identifier = str(uuid4())
        task = "Report when the new library reading room opens."
        instruction = "Publish every report as a newspaper PDF."
        revised_task = f"{task}\n\n{instruction}"
        goals = Goals(self.store)
        goals.save(identifier, {"title": "Morning check-in", "prompt": task, "zone": "UTC",
                                "frequency_minutes": 1440, "scheduled_at": anchor.isoformat()})
        question = "Create PDFs for every Morning check-in from now on."

        def edit(messages, *_args, **_options):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            if prompt == GOAL_INTENT:
                self.assertEqual(data["latest_message"], question)
                return "DOCUMENT"
            if prompt == CHOOSE_DOCUMENT_ACTION:
                self.assertEqual(data["question"], question)
                return "FORMAT"
            if prompt == SELECT_GOAL:
                return '{"target":1,"title":"Morning check-in"}'
            if prompt.startswith(EDIT_GOAL):
                self.assertEqual(data["selected_goal"]["task"], task)
                self.assertTrue(data["format_only"])
                return json.dumps({"format_instruction": instruction})
            if prompt == VERIFY_CHANGE:
                self.assertEqual(data["proposed_operation"], {"edit": {"format_instruction": instruction}})
                return "MATCH"
            if prompt == VERIFY_GOAL_FORMAT:
                return "YES"
            self.assertEqual(prompt, WRITE_GOAL_FORMAT)
            self.assertEqual(data["tools"]["goal"]["format_instruction"], instruction)
            return "Updated the format for the Morning check-in."

        self.ask(question, edit)
        saved = goals.detail(identifier)
        self.assertEqual(saved["prompt"], revised_task)
        self.assertEqual(saved["frequency_minutes"], 1440)
        self.assertEqual(datetime.fromisoformat(saved["next_run"]), anchor)

        def generate(messages, _tokens):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["goal"], revised_task)
            if prompt == PLAN and calls == 0:
                return tools({"name": "web"}, {"name": "web"})
            if prompt == RETRY_PLAN:
                return tools("create_pdf")
            if prompt == PLAN:
                return tools({"name": "create_pdf", "query": "newspaper"})
            if prompt.startswith(EXECUTE):
                return "NO_UPDATE" if calls == 0 else STORY
            if prompt == VERIFY_SKIP_PDF:
                self.assertEqual(calls, 0)
                return "NO"
            if prompt == WRITE_DOCUMENT:
                self.assertEqual(data["verified_update"], "" if calls == 0 else STORY)
                return json.dumps(EDITION | {"sections": [{"paragraphs": [STORY]}]})
            self.assertEqual(prompt, WRITE_NOTE)
            return "The update is in Files and the goal's Artifacts."

        runner = Goals(self.store, generate=generate)
        for calls in (0, 1):
            due = anchor + timedelta(days=calls)
            runner.run(due)
            runner.run(due + timedelta(minutes=1))
            files = artifacts(self.store, identifier)
            self.assertEqual(len(files), calls + 1)
            self.assertEqual(files, artifacts(self.store))
