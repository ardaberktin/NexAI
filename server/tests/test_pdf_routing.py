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
    REVIEW_PDF_DELIVERY,
)
from nex.conversation.goal_management_prompts import (
    EDIT_GOAL,
    SELECT_PDF_GOAL,
    VERIFY_PDF_CHANGE,
)
from nex.conversation.reply import (
    VERIFY_GOAL,
    VERIFY_GOAL_FORMAT,
    WRITE,
    WRITE_GOAL,
    WRITE_GOAL_FORMAT,
)
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.goal_runner import EXECUTE
from nex.goal_update_pdfs import CHOOSE_COPY_LAYOUT, deliver_update_pdfs
from nex.goals import Goals
from nex.pdf.artifacts import content
from nex.pdf.web import VERIFY_DOCUMENT_WEB, WRITE_WEB_DOCUMENT
from nex.privacy import public
from nex.scheduled_plan import PLAN
from nex.store import Store
from nex.web_html import Page
from nex.web_transport import WebReadError
from nex.workspace_content import artifacts, chat_history

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
                    if prompt.startswith(ROUTE_HEADER):
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
            if prompt.startswith(ROUTE_HEADER):
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
            if prompt.startswith(ROUTE_HEADER):
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
        goals = Goals(self.store)
        goals.save(identifier, {"title": "Morning check-in", "prompt": task, "zone": "UTC",
                                "frequency_minutes": 1440, "scheduled_at": anchor.isoformat()})
        question = "Create PDFs for every Morning check-in from now on."

        def edit(messages, *_args, **_options):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            if prompt.startswith(ROUTE_HEADER):
                self.assertEqual(data["latest_message"], question)
                return "DOCUMENT"
            if prompt == CHOOSE_DOCUMENT_ACTION:
                self.assertEqual(data["question"], question)
                return "REPEAT"
            if prompt.startswith(PLAN_PDF_GOAL):
                return tools({"name": "goal", "query": task, "start": anchor.strftime("%Y-%m-%dT%H:%M")})
            if prompt == REVIEW_PDF_DELIVERY:
                self.assertEqual(data["question"], question)
                return '{"action":"FORMAT"}'
            if prompt == SELECT_PDF_GOAL:
                self.assertEqual(data["goals"][0]["title"], "Morning check-in")
                self.assertEqual(data["goals"][0]["frequency"], 1440)
                return '{"target":1,"title":"Morning check-in"}'
            if prompt.startswith(EDIT_GOAL):
                self.assertEqual(data["selected_goal"]["task"], task)
                self.assertTrue(data["format_only"])
                return json.dumps({"format_instruction": instruction})
            if prompt == VERIFY_PDF_CHANGE:
                self.assertEqual(data["proposed_operation"], {"edit": {"format_instruction": instruction}})
                return "YES"
            if prompt == VERIFY_GOAL_FORMAT:
                return "YES"
            self.assertEqual(prompt, WRITE_GOAL_FORMAT)
            self.assertEqual(data["tools"]["goal"]["format_instruction"], instruction)
            return "Updated the format for the Morning check-in."

        saved_receipt = self.ask(question, edit)
        saved = goals.detail(identifier)
        self.assertEqual(saved_receipt["goal_id"], identifier)
        self.assertEqual(len(goals.overview()), 1)
        self.assertEqual(saved["prompt"], task)
        self.assertEqual(saved["pdf_instruction"], instruction)
        self.assertEqual(saved["frequency_minutes"], 1440)
        self.assertEqual(datetime.fromisoformat(saved["next_run"]), anchor)

        def generate(messages, _tokens):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["goal"], task)
            if prompt == PLAN:
                return '{"sources":["none"]}'
            self.assertEqual(prompt, EXECUTE)
            return "NO_UPDATE" if calls == 0 else STORY

        runner = Goals(self.store, generate=generate)
        calls = 0
        runner.run(anchor)
        self.assertEqual(artifacts(self.store, identifier), [])
        calls = 1
        due = anchor + timedelta(days=1)
        runner.run(due)
        text_update, = [row for row in chat_history(self.store) if row.get("goal_id") == identifier]
        self.assertEqual(text_update["text"], STORY)
        self.assertNotIn("pdf_attachment", text_update)
        with self.assertLogs("nex.goal_update_pdfs", level="WARNING"):
            deliver_update_pdfs(self.store, lambda *_: None, due)
        self.assertEqual(goals.detail(identifier)["latest"], STORY)
        self.assertIsNone(goals.detail(identifier)["error"])

        def layout(messages, _tokens):
            self.assertEqual(messages[0]["content"], CHOOSE_COPY_LAYOUT)
            payload = json.loads(messages[1]["content"])
            self.assertEqual(payload["document_brief"], instruction)
            return json.dumps({"title": EDITION["title"], "layout": "newspaper",
                               "sections": [{"paragraphs": ["This text was never published."]}]})

        deliver_update_pdfs(self.store, layout, due + timedelta(minutes=5))
        deliver_update_pdfs(self.store, layout, due + timedelta(minutes=10))
        item, = artifacts(self.store, identifier)
        self.assertEqual(artifacts(self.store), [item])
        self.assertEqual(item["id"], text_update["id"])
        with PdfDocument(content(self.store, item["id"])) as pdf:
            actual = pdf[0].get_textpage().get_text_bounded()
            self.assertIn(STORY, " ".join(actual.split()))
            self.assertNotIn("never published", actual)
        message, = [row for row in chat_history(self.store) if row.get("goal_id") == identifier]
        self.assertEqual(message["text"], STORY)
        self.assertEqual(message["pdf_attachment"]["id"], item["id"])
        self.assertEqual(datetime.fromisoformat(goals.detail(identifier)["next_run"]), due + timedelta(days=1))
