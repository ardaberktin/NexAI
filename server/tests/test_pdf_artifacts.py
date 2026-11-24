import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from pypdfium2 import PdfDocument

from conversation_fixtures import unlock
from nex.assistant import Assistant
from nex.conversation.catalog import CLARITY, INSTRUCTIONS, REVIEW_SCHEDULE
from nex.conversation.goal_management_prompts import SELECT_GOAL
from nex.conversation.pdf_tool import save
from nex.conversation.plan import choose, validated
from nex.conversation.reply import WRITE, run
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.conversation.sync import make_turn
from nex.goals import Goals
from nex.http_api import API
from nex.pdf.artifacts import content
from nex.pdf.document import WRITE_DOCUMENT, Document, PDFError
from nex.pdf.render import render
from nex.privacy import public, requests
from nex.privacy.state import AccessDenied
from nex.store import Store
from nex.workspace_chat_pages import chat_page
from nex.workspace_content import artifacts, chat_history
from nex.workspace_schema import WorkspaceMissing

EDITION = {
    "title": "The Morning Edition", "subtitle": "Monday, November 23, 2026 · Victoria", "layout": "newspaper",
    "sections": [
        {"heading": "Today’s commitments", "paragraphs": ["A planning meeting starts at 9:00 a.m. in the library.",
                                                          "Submit the draft outline by 5:00 p.m."]},
        {"heading": "In the forecast", "paragraphs": ["Pack a jacket for the morning walk."]},
        {"heading": "Around town", "paragraphs": ["The library opens its new reading room this afternoon."]},
    ],
}


class PDFRenderTests(unittest.TestCase):
    def test_newspaper_uses_two_columns_and_preserves_words_across_pages(self):
        text = "İstanbul, Şişli ve İzmir’de görüşmeler."
        long = "Packing lists should include seasonal gear. " * 52
        doc = EDITION | {"sections": [*EDITION["sections"],
                                      {"heading": "More reading", "paragraphs": [text, long] * 3}]}
        rendered = render(Document.read(json.dumps(doc)))
        self.assertGreater(rendered.pages, 1)
        with PdfDocument(rendered.data) as pdf:
            self.assertEqual(len(pdf), rendered.pages)
            self.assertTrue(pdf[0].get_textpage().get_text_bounded(left=320).strip())
            extracted = " ".join(" ".join(page.get_textpage().get_text_bounded().split()) for page in pdf)
            self.assertEqual(extracted.count(text), 3)
            self.assertEqual(extracted.count("Packing lists should include seasonal gear."), 156)

    def test_single_column_report_preserves_an_unbroken_value(self):
        link = "https://example.test/" + "000xyz" * 36
        doc = EDITION | {"layout": "report", "sections": [{"heading": "Reference", "paragraphs": [link]}]}
        rendered = render(Document.read(json.dumps(doc)))
        self.assertEqual(rendered.pages, 1)
        with PdfDocument(rendered.data) as pdf:
            text = pdf[0].get_textpage().get_text_bounded()
            self.assertIn(link, "".join(text.split()))

    def test_invalid_or_unavailable_glyphs_are_rejected(self):
        with self.assertRaisesRegex(PDFError, "supported text length"):
            Document.read(json.dumps(EDITION | {"sections": [{"paragraphs": ["a" * 2500] * 5}]}))
        with self.assertRaisesRegex(PDFError, "embedded PDF fonts"):
            render(Document.read(json.dumps(EDITION | {"title": "Morning ☃"})))
        with (patch("nex.pdf.render.validate", side_effect=PDFError("validation_failed", "Unexpected overlap.")),
              self.assertRaisesRegex(PDFError, "Unexpected overlap")):
            render(Document.read(json.dumps(EDITION)))


class PDFWorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(folder, 0, 2**20)
        self.anchor = datetime(2026, 11, 23, 17, tzinfo=UTC)
        self.goal = str(uuid4())
        self.goals = Goals(self.store)
        self.session = unlock(self.store)

    def save_goal(self):
        self.goals.save(self.goal, {"title": "Morning check-in", "prompt": "Publish a morning check-in as a newspaper PDF",
                                    "zone": "UTC", "frequency_minutes": 1440,
                                    "scheduled_at": (self.anchor + timedelta(hours=1)).isoformat()}, anchor=self.anchor)

    def test_chat_uses_the_model_and_saves_only_after_unlock(self):
        def generate(messages, *_args, **_options):
            prompt = messages[0]["content"]
            payload = json.loads(messages[1]["content"])
            if prompt.startswith(ROUTE_HEADER):
                return "OTHER"
            if prompt.startswith((INSTRUCTIONS, REVIEW_SCHEDULE)):
                seen.append(payload["question"])
                calls = ({"name": "create_pdf"}, {"name": "agenda", "query": "today"})
                if payload["question"] == "What makes a good newspaper layout?":
                    calls = ()
                return json.dumps({"tools": calls})
            if prompt == WRITE_DOCUMENT:
                self.assertEqual(payload["question"], question)
                self.assertIn("agenda", payload["evidence"])
                return json.dumps(EDITION)
            if prompt.startswith(WRITE):
                outcome = payload["tools"].get("create_pdf")
                if outcome:
                    self.assertEqual(outcome["status"], "saved")
                    return "The Morning Edition is in Files."
                return "Here are some newspaper layout ideas."
            self.fail(prompt)

        assistant = Assistant(self.store, SimpleNamespace(generate=generate), None)
        seen = []
        for question in ("Make a PDF of my check-in, laid out like a newspaper.",
                         "Print my morning agenda as a newspaper-style document."):
            identifier = str(uuid4())
            pending = public.chat(assistant, identifier, "mac", question, "en", "UTC")
            self.assertEqual(pending["engine"], "private_request")
            queued = next(item for item in requests.inbox(self.store.privacy, self.store)["items"]
                          if item["id"] == identifier)
            self.assertNotIn("pdf_attachment", queued)
            before = len(artifacts(self.store))
            public.chat(assistant, identifier, "mac", question, "en", "UTC", session=self.session)
            public.chat(assistant, identifier, "mac", question, "en", "UTC", session=self.session)
            files = artifacts(self.store)
            self.assertEqual(len(files), before + 1)
            ready = next(item for item in requests.inbox(self.store.privacy, self.store)["items"]
                         if item["id"] == identifier)
            self.assertEqual(ready["state"], "ready")
            self.assertIn(ready["pdf_attachment"], files)
            self.assertEqual(ready["pdf_attachment"]["page_count"], 1)
        self.assertEqual(chat_page(self.store)["items"], [])
        question = "What makes a good newspaper layout?"
        public.chat(assistant, str(uuid4()), "mac", question, "en", "UTC")
        self.assertEqual(seen[-1], question)
        self.assertEqual(len(artifacts(self.store)), 2)
        self.assertTrue(all(item["state"] == "ready" for item in requests.inbox(self.store.privacy)["items"]))

    def test_file_download_requires_an_owner_session_and_preserves_pdf_bytes(self):
        pdf = render(Document.read(json.dumps(EDITION)))
        identifier = str(uuid4())
        save(self.store, identifier, None, pdf)
        endpoint = f"/v2/workspace/artifacts/{identifier}/pdf"
        handler = API.__new__(API)
        handler.server = SimpleNamespace(app=SimpleNamespace(store=self.store), privacy=self.store.privacy)
        handler.send = Mock()
        handler.headers = {}
        with self.assertRaises(AccessDenied):
            handler.dispatch("GET", endpoint, {}, "mac")
        handler.send.assert_not_called()
        handler.headers["X-Nex-Workspace"] = self.session
        handler.dispatch("GET", endpoint, {}, "mac")
        handler.send.assert_called_once_with(pdf.data, mime="application/pdf")

    def test_private_voice_picks_the_pdf_tool_and_file_survives_missing_final_reply(self):
        turn = make_turn(str(uuid4()), "iphone", "Can you make my notes into a PDF?", "en", "UTC", spoken=True)
        async def select(messages, _tokens):
            if messages[0]["content"] == CLARITY:
                return "CLEAR"
            self.assertEqual(json.loads(messages[1]["content"])["question"], turn.text)
            return '{"tools":[{"name":"create_pdf"}]}'

        plan = asyncio.run(choose(turn, select))
        self.assertTrue(plan.private)
        with self.assertRaises(PermissionError):
            asyncio.run(run(self.store, turn, plan, None))

        async def generate(messages, _tokens):
            system = messages[0]["content"]
            if system == WRITE_DOCUMENT:
                return json.dumps(EDITION)
            return None

        first = asyncio.run(run(self.store, turn, plan, generate, private=True))
        self.assertEqual(first["engine"], "model_unavailable")
        second = asyncio.run(run(self.store, turn, plan, generate, private=True))
        self.assertEqual(second["engine"], "model_unavailable")
        self.assertEqual(len(artifacts(self.store)), 1)

    def test_existing_goal_can_supply_content_for_a_pdf_without_changing_its_schedule(self):
        self.save_goal()
        with self.store.connect() as db:
            db.execute("UPDATE goals SET latest=? WHERE id=?", ("A meeting at 9:00 a.m.", self.goal))
        history = (
            {"kind": "question", "text": "What is the latest from Morning check-in?", "private": True},
            {"kind": "answer", "text": "A meeting at 9:00 a.m.", "private": True, "goal_id": self.goal},
        )
        turn = make_turn(str(uuid4()), "mac", "Turn that into a PDF.", "en", "UTC", history=history)
        plan = validated([{"name": "create_pdf", "goal_query": "Morning check-in"}])

        async def generate(messages, _tokens):
            system = messages[0]["content"]
            payload = json.loads(messages[1]["content"])
            if system == SELECT_GOAL:
                self.assertEqual(payload["goals"][0]["title"], "Morning check-in")
                return '{"target":1,"title":"Morning check-in"}'
            if system == WRITE_DOCUMENT:
                self.assertEqual(payload["saved_goal"]["latest"], "A meeting at 9:00 a.m.")
                self.assertEqual(payload["history"][-1]["content"], payload["saved_goal"]["latest"])
                return json.dumps(EDITION)
            return "The PDF is in the goal’s Artifacts."

        before = self.goals.detail(self.goal)
        asyncio.run(run(self.store, turn, plan, generate, private=True))
        item, = artifacts(self.store, self.goal)
        self.assertEqual(artifacts(self.store), [item])
        after = self.goals.detail(self.goal)
        self.assertEqual(after["artifact_count"], 1)
        self.assertEqual(after["revision"], before["revision"])

    def test_scheduled_pdf_and_update_commit_once_and_are_deleted_with_the_goal(self):
        self.save_goal()
        due = self.anchor + timedelta(hours=1)
        generate = Mock(side_effect=[
            '{"tools":[{"name":"create_pdf","query":"newspaper"},{"name":"agenda","query":"today"}]}',
            "A planning meeting starts at 9:00 a.m.", json.dumps(EDITION),
            "The Morning Edition is available in Files and this goal’s Artifacts.",
        ])
        runner = Goals(self.store, generate=generate)
        runner.run(due)
        runner.run(due + timedelta(minutes=1))
        self.assertEqual(generate.call_count, 4)
        context = json.loads(generate.call_args_list[2].args[0][1]["content"])
        self.assertIn("agenda", context["evidence"])
        self.assertEqual(context["document_brief"], "newspaper")
        file, = artifacts(self.store)
        self.assertEqual(artifacts(self.store, self.goal), [file])
        update, = chat_history(self.store)
        self.assertEqual(update["id"], file["id"])
        self.assertIn("Artifacts", update["text"])
        self.assertEqual(update["pdf_attachment"]["id"], file["id"])
        self.assertEqual(update["pdf_attachment"]["text"], "")
        self.assertEqual(chat_page(self.store)["items"], [update])
        self.assertEqual(self.goals.detail(self.goal)["artifact_count"], 1)
        self.assertTrue(content(self.store, file["id"]).startswith(b"%PDF"))
        self.goals.delete(self.goal, self.goals.detail(self.goal)["revision"])
        self.assertEqual(artifacts(self.store), [])
        with self.assertRaises(WorkspaceMissing):
            content(self.store, file["id"])

    def test_failed_validation_does_not_publish_a_scheduled_file_or_update(self):
        self.save_goal()
        due = self.anchor + timedelta(hours=1)
        generate = Mock(side_effect=[
            '{"tools":[{"name":"create_pdf"}]}', "Synthetic report", json.dumps(EDITION), json.dumps(EDITION),
        ])
        with patch("nex.pdf.render.validate", side_effect=PDFError("validation_failed", "Unexpected overlap.")):
            Goals(self.store, generate=generate).run(due)
        self.assertEqual(artifacts(self.store), [])
        self.assertEqual(chat_history(self.store), [])
        goal = self.goals.detail(self.goal)
        self.assertEqual(goal["error"], "Unexpected overlap.")
        self.assertEqual(datetime.fromisoformat(goal["next_run"]), due + timedelta(minutes=5))
