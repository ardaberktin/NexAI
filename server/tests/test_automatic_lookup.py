import tempfile
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from conversation_fixtures import model
from nex.assistant import Assistant
from nex.settings import Settings
from nex.store import Store
from nex.voice_lookup import lookup_query
from nex.web_grounding import supported_answer
from nex.web_html import Page
from nex.web_intent import web_request
from nex.web_transport import WebReadError


class AutomaticLookupTests(unittest.TestCase):
    def test_factual_questions_do_not_need_search_instructions(self):
        cases = {
            "Where can I play GTA Six?": "GTA 6 platforms",
            "Which platforms is GTA 6 available on?": "GTA 6 platforms",
            "Can I play GTA 6 on PC?": "GTA 6 platforms on PC",
            "Where can we watch Severance?": "Severance where to watch",
            "Could you tell me who wrote Middlemarch?": "who wrote Middlemarch",
            "How do I install Firefox?": "how to install Firefox",
            "Do I need a visa to visit Japan?": "does someone need a visa to visit Japan",
            "What is the capital of Kenya?": "What is the capital of Kenya",
            "Steam Deck system requirements?": "Steam Deck system requirements",
        }
        for text, terms in cases.items():
            with self.subTest(text=text):
                self.assertEqual(web_request(lookup_query(text)).terms, terms)

    def test_only_public_topics_leave_personal_and_creative_conversations(self):
        for text in ("Where can I find my notes?", "Where can I play the confidential demo?",
                     "How do I recover my API key?", "What can you see on my screen?",
                     "Can you write a poem about Mars?", "Write a story about streaming platforms.",
                     "What if dragons were real?", "Which tab is active?",
                     "What is 7 + 5?", "What's your name?", "What AI model are you?",
                     "Without searching, what is the capital of Kenya?"):
            with self.subTest(text=text):
                self.assertIsNone(lookup_query(text))
        history = [{"kind": "question", "text": "What’s on my calendar today?"},
                   {"kind": "answer", "engine": "account_read", "text": "A private appointment."}]
        self.assertIsNone(lookup_query("Are you sure?", history))

    def test_new_followup_replaces_the_previous_country(self):
        history = []
        for question in ("Where can I watch Severance?", "What about Canada?", "Are you sure?"):
            history.extend([{"kind": "question", "text": question},
                            {"kind": "answer", "text": "See the listing.", "engine": "web_lookup"}])
        self.assertEqual(web_request(lookup_query("What about France?", history)).terms,
                         "Severance where to watch France")
        self.assertEqual(web_request(lookup_query("How much does it cost?", history)).terms, "Severance price")

    def test_actual_chat_and_spoken_followups_refetch_the_named_public_topic(self):
        page = Page("Publisher listing", "https://example.org/game", "Coming to PS5 and Xbox Series X|S.")
        engine = Mock()
        engine.generate.return_value = "Coming to PS5 and Xbox Series X|S. [1]"
        with tempfile.TemporaryDirectory() as root, patch("nex.web_answers.lookup", return_value=[page]) as search:
            assistant = Assistant(Store(root, 0, 2**20), engine, Settings(root))

            def choose(data):
                text = data["question"]
                if "your name" in text or text == "Are you sure?":
                    return []
                if "remember" in text:
                    return [{"name": "memory"}]
                query = "GTA 6 platforms on PC" if text == "Can I play it on PC?" else "GTA 6 platforms"
                return [{"name": "web", "query": query}]
            assistant.generate_conversation = model(choose, compose="Available on PS5. [1]")

            def ask(text):
                return assistant.chat(text, "en", "mac", str(uuid4()))

            self.assertEqual(ask("Where can I play GTA 6?")["engine"], "web_lookup")
            ask("What's your name?")
            self.assertEqual(search.call_count, 1)
            for text in ("Ser GTA Six can you repeat that again", "No you're wrong. You can't play Grand of Sex on PC right now",
                         "No it's only available for PS Five and Xbox"):
                self.assertEqual(ask(text)["engine"], "web_lookup")
            assistant.answer("Can I play it on PC?", "en", spoken=True)
            self.assertEqual([c.args[0].terms for c in search.call_args_list],
                             ["GTA 6 platforms"] * 4 + ["GTA 6 platforms on PC"])
            ask("What do you remember about me?")
            ask("Are you sure?")
            self.assertEqual(search.call_count, 5)

    def test_typed_failure_does_not_invent_an_answer_and_a_citation_cannot_add_stores(self):
        with tempfile.TemporaryDirectory() as root, patch("nex.web_answers.lookup", side_effect=WebReadError("Unavailable")):
            assistant = Assistant(Store(root, 0, 2**20), Mock(), SimpleNamespace(read=dict))
            def compose(data):
                self.assertEqual(data["tools"]["web"], {"unavailable": "Unavailable"})
                return "I couldn't reach the search provider."

            assistant.generate_conversation = model([{"name": "web", "query": "GTA 6 platforms"}], compose=compose)
            result = assistant.answer("Where can I play GTA 6?", "en")
            self.assertEqual((result["text"], result["engine"]), ("I couldn't reach the search provider.", "web_lookup"))
            assistant.engines.generate.assert_not_called()
        pages = [{"number": 1, "title": "Publisher listing", "excerpt": "Coming to PlayStation 5 and Xbox Series X|S."}]
        self.assertFalse(supported_answer("Available for PC and Mac via Steam. [1]", pages))
        self.assertFalse(supported_answer("PlayStation Plus and Game Pass. [1]", pages, "GTA 6 platforms"))
        self.assertFalse(supported_answer("It is available on Xbox One. [1]", pages))
        self.assertTrue(supported_answer("Coming to PS5 and Xbox Series X|S. [1]", pages, "GTA 6 platforms"))

    def test_screen_sharing_keeps_visual_questions_local_and_retries_a_public_turn_only_once(self):
        page = Page("Launch platforms", "https://example.org/game", "Available on PlayStation 5.")
        engine = Mock()
        engine.generate = model(lambda p: [{"name":"web", "query":"GTA 6 platforms"}] if "GTA" in p["question"] else [], compose="Available on PS5. [1]")
        with tempfile.TemporaryDirectory() as root, patch("nex.web_answers.lookup", return_value=[page]) as search:
            store = Store(root, 0, 2**20)
            assistant = Assistant(store, engine, Settings(root))
            identifier = str(uuid4())
            args = ("Where can I play GTA 6?", "en", "mac", identifier)
            result = assistant.chat(*args, lookup_only=True)
            self.assertEqual(result["engine"], "web_lookup")
            self.assertEqual(assistant.chat(*args, lookup_only=True), result)
            screen_id = str(uuid4())
            self.assertIsNone(assistant.chat("What's on my screen?", "en", "mac", screen_id, lookup_only=True))
            self.assertIsNone(store.source("q-" + screen_id))
            self.assertIsNone(assistant.chat("Are you sure?", "en", "mac", str(uuid4()), lookup_only=True,
                                             after=datetime.now(UTC)))
            search.assert_called_once()

    def test_matching_support_faq_answers_a_platform_question_ahead_of_a_roundup(self):
        listing = "You can pre-order Grand Theft Auto VI for PlayStation 5 and Xbox Series X|S."
        article = Page("Community roundup", "https://example.org/guide", "A PC release on Steam is rumoured.")
        support = Page("Grand Theft Auto VI platforms", "https://support.example.com/game",
                       "What platforms will Grand Theft Auto V be on?\nIt can be played on PC.\n"
                       "What platforms will Grand Theft Auto VI be on?\n" + listing + "\n"
                       "How do download codes work?\nRedeem the code on the platform account.")
        with tempfile.TemporaryDirectory() as root, patch("nex.web_answers.lookup", return_value=[article, support]):
            engine = Mock()
            assistant = Assistant(Store(root, 0, 2**20), engine, Settings(root))
            engine.generate = model([{"name": "web", "query": "GTA 6 platforms"}], compose=listing + " [2]")
            for spoken in (True, False):
                result = assistant.answer("Where can I play GTA 6?", "en", spoken=spoken)
                self.assertEqual(result["text"], listing + " [2]")
                self.assertEqual(len(result["sources"]), 2)
                self.assertIn(listing, engine.generate.call_args.args[0][1]["content"])
