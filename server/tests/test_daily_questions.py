import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import Mock, patch
from uuid import uuid4

from conversation_fixtures import model
from nex.accounts.intent import account_intent
from nex.assistant import Assistant
from nex.daybook import Daybook, recap_context
from nex.goals import Goals
from nex.recap_translation import grounded_translation, translation_candidate
from nex.store import Store
from nex.time_budget import time_budget
from nex.voice_lookup import voice_lookup_query


class DailyQuestionsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name, 0, 2**20)
        self.assistant = Assistant(self.store, Mock(), Mock())
        self.zone = "America/Vancouver"
        self.anchor = datetime(2026, 11, 22, 2, tzinfo=UTC)

    def test_natural_agenda_is_private_and_explicit_provider_queries_still_work(self):
        for phrase in ("What do I have to do today, Nex?", "What do I need to do today?",
                       "What's on my plate tomorrow?", "Could you tell me what I have to do today?"):
            self.assertEqual(account_intent(phrase).provider, "agenda")
            self.assertIsNone(voice_lookup_query(phrase))
        self.assertEqual(account_intent("What's on my calendar today?").provider, "calendar")
        self.assertEqual(account_intent("List my tasks today.").provider, "ticktick")

    def test_agenda_uses_local_date_and_keeps_other_sources_on_a_partial_failure(self):
        for title, day in (("Blue atlas", 22), ("Tomorrow only", 23)):
            Goals(self.store).save(str(uuid4()), {"prompt": title, "zone": self.zone,
                                   "scheduled_at": f"2026-11-{day}T05:30:00+00:00"}, anchor=self.anchor)

        def read(_root, intent, _query, _zone, _reference):
            if intent.provider == "calendar":
                return "Google Calendar", [{"start": "2026-11-21 10:30", "title": "Design review"}], ""
            return "TickTick", [], "Reconnect TickTick under Connected accounts in Nex Settings."

        self.assistant.generate_conversation = model([{"name": "agenda", "query": "today"}])
        with patch("nex.accounts.agenda.prepare_account", side_effect=read), patch("nex.web_answers.lookup", side_effect=AssertionError):
            answer = self.assistant.answer("What do I need to do today?", "en", zone=self.zone,
                                           asked_at=self.anchor, spoken=True)
        self.assertEqual(answer["engine"], "account_read")
        text = answer["text"]
        self.assertLess(text.index("Design review"), text.index("Blue atlas"))
        self.assertIn("Reconnect TickTick", text)
        self.assertNotIn("Tomorrow only", text)
        self.assertEqual(self.store.search("Design review"), [])

    def test_recap_uses_daybook_and_abstains_on_an_empty_day(self):
        self.assistant.generate_conversation = model([{"name": "daybook"}])
        question = "Summarize my day"
        self.assertEqual(recap_context(question, self.zone, self.anchor).day, "2026-11-21")
        self.assertIsNone(recap_context("Summarize today's emails", self.zone))
        answer = self.assistant.answer(question, "en", zone=self.zone, asked_at=self.anchor, spoken=True)
        self.assertEqual(answer["engine"], "archive_search")
        self.assertEqual(answer["sources"], [])
        self.assistant.engines.generate_daybook.assert_not_called()

    def test_bilingual_recap_quotes_available_speech_and_preserves_citation(self):
        key = str(uuid4())
        recorded = "I finished the cedar prototype and sent the draft to Noor."
        self.store.save_recording(key, "mac", self.anchor.isoformat(), 10, "ambient", False, "en", b"test")
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state='ready',transcript=? WHERE id=?", (recorded, key))
        translation = "Sedir prototipini bitirdim ve taslağı Noor'a gönderdim."
        self.assistant.generate_conversation = model([{"name": "daybook"}],
                                                     language=lambda p: "tr" if "Bugün" in p["latest_message"] else "en",
                                                     compose=lambda p: translation if p["language"] == "tr" else recorded)
        for query in ("Bugün neler konuştuk?", "What did we discuss today?"):
            answer = self.assistant.answer(query, "auto", zone=self.zone, asked_at=self.anchor)
            self.assertEqual(answer["sources"], [key])
            self.assertEqual(answer["text"], translation if "Bugün" in query else recorded)
        self.assertEqual(recap_context("Dün ne yaptık?", self.zone, self.anchor).day, "2026-11-20")
        source = {"text": recorded, "language": "en", "sources": [key], "engine": "archive_search"}
        self.assertIsNone(translation_candidate(source, "tr", "Saat 9'da görüştük."))
        garbled = translation_candidate(source, "tr", "Işığı kırık yaparak korkutuldu.")
        self.assertEqual(grounded_translation(source, garbled, "He was scared by the broken light."), source)
        candidate = translation_candidate(source, "tr", translation)
        self.assertEqual(grounded_translation(source, candidate, "I completed a lamp for Noor."), source)
        scheduled = source | {"text": "Rin moved the review to Tuesday."}
        tuesday = translation_candidate(scheduled, "tr", "Rin toplantıyı salı gününe aldı.")
        self.assertEqual(grounded_translation(scheduled, tuesday, scheduled["text"]), tuesday)

    def test_time_budget_uses_both_explicit_durations(self):
        question = "I have 35 minutes before the train and it takes 12 minutes to walk there. How many minutes can I wait?"
        self.assertEqual(time_budget(question), 23)
        self.assertEqual(time_budget("It starts in 1 hour and takes 70 minutes to reach. How long can I wait?"), -10)
        self.assertIsNone(time_budget(question.replace("takes", "doesn't take")))
        self.assertIsNone(time_budget(question.replace("12 minutes", "12 minutes or 20 minutes")))

    def test_pending_briefing_quotes_a_recording_only_once(self):
        day = recap_context("Summarize my day", self.zone, self.anchor)
        evidence = [{"id": "a", "kind": "recording", "text": "Rin sent the draft."},
                    {"id": "a", "kind": "activity", "text": "09:00 · Conversation. Rin sent the draft."}]
        book = Daybook(self.store)
        with patch.object(book, "evidence", return_value=evidence):
            answer = book.ready_recap(day)
        self.assertEqual(answer["text"], "Rin sent the draft.")
        self.assertEqual(answer["sources"], ["a"])
