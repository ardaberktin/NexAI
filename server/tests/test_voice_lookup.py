import tempfile
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conversation_fixtures import model
from nex.assistant import Assistant
from nex.store import Store
from nex.voice_lookup import public_question, voice_lookup_query
from nex.web_answers import finish_lookup
from nex.web_grounding import calendar_dates, supported_answer
from nex.web_html import Page
from nex.web_intent import web_request
from nex.web_transport import WebReadError


class VoiceLookupTests(unittest.TestCase):
    def test_release_date_paraphrases_and_named_public_topics(self):
        for phrase in ("When is GTA Six coming out", "Next, how long until GTA 6 comes out?",
                       "Look it up yourself and find me when GTA Six is coming out",
                       "Could you check when GTA 6 will be released?", "What's the release date for GTA 6?"):
            with self.subTest(phrase=phrase):
                self.assertEqual(web_request(voice_lookup_query(phrase)).terms, "GTA 6 release date")
        for phrase in ("Who won the World Series?", "What's the current price of copper?",
                       "When is the next NASA launch?", "GTA 6 ne zaman çıkacak?"):
            self.assertIsNotNone(voice_lookup_query(phrase))

    def test_confirmations_only_reuse_a_public_question_from_this_exchange(self):
        history = [{"kind": "question", "text": "When is GTA Six coming out?"},
                   {"kind": "answer", "text": "Would you like me to check a reliable source for you?"}]
        for phrase in ("Yes, please", "Look it up yourself", "When is it coming out?"):
            self.assertEqual(web_request(voice_lookup_query(phrase, history)).terms, "GTA 6 release date")
        history[1]["text"] = "How are you?"
        history[0]["web_query"] = "Search the web for GTA 6 release date"
        self.assertIsNone(voice_lookup_query("Yes, please", history))
        history[0].pop("web_query")
        history[0]["text"] = "What's on my calendar today?"
        with self.assertRaises(WebReadError):
            web_request(voice_lookup_query("Look it up", history))
        self.assertIsNone(voice_lookup_query("Yes, please"))

    def test_private_context_stays_out_of_both_automatic_routes(self):
        for phrase in ("What did I ask yesterday?", "What is my current focus?", "What's on my screen today?",
                       "What is the latest inbox message?", "When is the confidential launch coming out?",
                       "What is the score today? My password is secret.", "What is my API key?",
                       "What did we record today?", "When is 123 Main Street coming out?"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(voice_lookup_query(phrase))
                self.assertIsNone(public_question(phrase))

    def test_recorded_voice_gives_the_search_failure_to_the_model(self):
        with tempfile.TemporaryDirectory() as root:
            assistant = Assistant(Store(root, 0, 2**20), Mock(), SimpleNamespace(read=dict))
            def compose(data):
                self.assertEqual(data["tools"]["web"], {"unavailable": "The search provider timed out."})
                return "The lookup timed out before I could verify a release date."

            assistant.generate_conversation = model([{"name": "web", "query": "GTA 6 release date"}], compose=compose)
            with patch("nex.web_answers.lookup", side_effect=WebReadError("The search provider timed out.")) as lookup:
                answer = assistant.answer("When is GTA Six coming out?", "en", spoken=True)
            self.assertEqual(lookup.call_args.args[0].terms, "GTA 6 release date")
            assistant.engines.generate.assert_not_called()
            self.assertEqual((answer["engine"], answer["text"]),
                             ("web_lookup", "The lookup timed out before I could verify a release date."))

    def test_recorded_local_weather_requests_a_city_instead_of_searching_for_unspecified_weather(self):
        with tempfile.TemporaryDirectory() as root:
            assistant = Assistant(Store(root, 0, 2**20), Mock(), SimpleNamespace(read=dict))
            assistant.generate_conversation = model([{"name": "weather", "period": "current"}])
            with patch("nex.web_answers.lookup") as search:
                result = assistant.answer("What's the weather today?", "en", spoken=True, device="iphone")
            self.assertEqual(result["engine"], "weather_city_needed")
            self.assertIn("city", result["text"])
            search.assert_not_called()
            assistant.engines.generate.assert_not_called()

    def test_verified_date_and_countdown_use_the_devices_calendar_day(self):
        evidence = [{"number": 1, "title": "The launch is November 19, 2026", "excerpt": "Official announcement."}]
        asked = datetime(2026, 11, 19, 2, tzinfo=UTC)
        for unsupported in ("It was released November 15, 2011. [1]", "November 19, 2026. [2]", "2011.",
                            "December 19. [1]", "I cannot check real-time information. [1]"):
            self.assertFalse(supported_answer(unsupported, evidence))
        self.assertTrue(supported_answer("The launch is November 19. [1]", evidence))
        result = finish_lookup(evidence, ["source"], "", "The launch is 19 November 2026. [1]", "en",
                               spoken=True, query="How long until launch?", asked_at=asked, zone="America/Vancouver")
        self.assertIn("tomorrow", result["text"])
        self.assertEqual(calendar_dates(result["text"]).pop().isoformat(), "2026-11-19")

    def test_a_model_cannot_speak_an_invented_date_before_citations_are_checked(self):
        page = Page("Studio confirms November 19, 2026", "https://example.org/news", "Launch on November 19, 2026.")
        with tempfile.TemporaryDirectory() as root, patch("nex.web_answers.lookup", return_value=[page]):
            engine, speech = Mock(), Mock()
            engine.generate = model([{"name": "web", "query": "GTA 6 release date"}], compose="The game was released on November 15, 2011. [1]")
            assistant = Assistant(Store(root, 0, 2**20), engine, SimpleNamespace(read=dict))
            result = assistant.answer("When is GTA Six coming out?", "en", spoken=True, on_speech=speech)
            self.assertEqual((result["engine"], result["text"]), ("web_verification_failed", ""))
            speech.assert_not_called()
