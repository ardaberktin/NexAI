import importlib.util
import tempfile
import unittest
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from conversation_fixtures import model, tools, unlock
from nex.assistant import Assistant
from nex.personal_age import calendar_age
from nex.store import Store
from nex.voice_lookup import lookup_query


class PersonalAgeTests(unittest.TestCase):
    zone = "America/Vancouver"
    # Still November 21 on the device.
    asked = datetime(2026, 11, 22, 4, 9, tzinfo=UTC)
    question = "How many months and days and minutes"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assistant = Assistant(Store(directory.name, 0, 2**20), Mock(), Mock())
        self.assistant.generate_conversation = model([{"name":"age"}])
        self.history = []
        self.search = self.enterContext(patch("nex.web_answers.lookup", side_effect=AssertionError("Unexpected lookup")))

    def chat(self, text, **kwargs):
        from nex.conversation.sync import execute, make_turn, select
        if text.startswith("Remember that "):
            calls = [{"name":"save_memory","query":text.removeprefix("Remember that ")}]
        elif text == "Show me your memory":
            calls = [{"name":"memory"}]
        else:
            calls = [{"name":"age"}]
        self.assistant.generate_conversation = model(calls)
        from nex.personal_age import datetime as clock
        anchor = clock.now(UTC) if isinstance(clock, Mock) else self.asked
        turn = make_turn(str(uuid4()), "mac", text, "en", self.zone, asked_at=anchor, history=self.history)
        plan = select(self.assistant, turn)
        if kwargs.get("lookup_only"):
            self.assertTrue(plan.private)
            return None
        result = execute(self.assistant, turn, plan, private=True)
        self.history.extend(({"kind":"question","text":text},{"kind":"answer", **result}))
        return result

    def answer(self, text, **kwargs):
        self.assistant.generate_conversation = model([{"name":"age"}])
        return self.assistant.answer(text, "en", zone=self.zone, asked_at=self.asked, **kwargs)

    def test_chat_uses_the_saved_birthday_on_the_first_followup(self):
        self.chat("Remember that I was born on April 16th, 2001.")
        # Freeze the calendar while exercising the normal, persisted chat exchange.
        with patch("nex.personal_age.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.asked
            self.assertIn("25 years old", self.chat("How old am I")["text"])
            result = self.chat(self.question)
            for fragment in ("November 21, 2026", "307 months", "5 days", "9,350", "13,464,000", "birth time"):
                self.assertIn(fragment, result["text"])
            self.assertEqual((result["engine"], result["sources"]), ("calculation", []))
            self.assertIn("9,350", self.chat("And in days?")["text"])
        self.search.assert_not_called()
        self.assertEqual(self.assistant.engines.mock_calls, [])

    def test_unclear_units_stay_local_and_public_questions_keep_their_topic(self):
        self.assertIsNone(lookup_query(self.question))
        self.assertIsNotNone(lookup_query("How many minutes are in a soccer match?"))
        self.chat("Remember that my birthday is April 16, 2001.")
        # A different current subject takes precedence over the saved birthday.
        self.chat("Show me your memory")
        self.assertIsNone(self.chat(self.question, lookup_only=True))
        self.assistant.generate_conversation = model([], compose="Which two dates should I use?")
        with patch.object(self.assistant.store, "search", side_effect=AssertionError("Unexpected archive search")):
            self.assertEqual(self.assistant.answer(self.question,"en")["text"], "Which two dates should I use?")

    def test_screen_chat_retries_the_same_calculation(self):
        from nex.privacy import public, requests
        self.chat("Remember that my date of birth is 2001-04-16.")
        self.assistant.generate_conversation = model([{"name":"age"}])
        store = self.assistant.store
        args = (self.assistant, str(uuid4()), "mac", "How old am I?", "en", self.zone)
        token = unlock(store)
        self.assertIsNone(public.chat(*args, lookup_only=True, session=token))
        first = public.chat(*args, session=token)
        self.assertEqual(first["engine"], "private_request")
        self.assertEqual(public.chat(*args, session=token), first)
        item, = requests.inbox(store.privacy)["items"]
        self.assertIn("years old", item["answer"])

    def test_older_model_exchange_and_recorded_voice_use_the_speakers_birthdate(self):
        history = [
            {"kind": "question", "text": "I was born on April 16th, 2001."},
            {"kind": "answer", "text": "Got it.", "engine": "local_model"},
            {"kind": "question", "text": "How old am I"},
            {"kind": "answer", "text": "You are currently 52 years old.", "engine": "local_model"},
        ]
        spoken = Mock()
        with patch("nex.assistant.recent_dialogue", return_value=history):
            result = self.answer(self.question, spoken=True, on_speech=spoken)
        self.assertIn("307 months", result["text"])
        spoken.assert_called_once_with(result["text"], "en")
        history += [{"kind": "question", "text": "Thanks!"}, {"kind": "answer", "text": "You're welcome."}]
        with patch("nex.assistant.recent_dialogue", return_value=history):
            self.assertIn("9,350 days", self.answer("And in days?")["text"])

    def test_missing_birthday_can_be_supplied_without_repeating_the_question(self):
        self.chat("Remember that my sister was born on April 16th, 2001.")
        first = self.chat("How many days old am I?")
        self.assertIn("date of birth", first["text"])
        result = self.chat("April 16th, 2001.")
        self.assertEqual(result["engine"], "calculation")
        self.assertIn("days", result["text"])
        self.assertIn("months", self.chat("What about months?")["text"])

    def test_reads_existing_markdown_without_a_recent_birthday_conversation(self):
        files = self.assistant.personal
        memory = files.read("memory")
        files.edit("memory", memory["revision"], "# Memory\n- My dog was born on May 1, 2019.\n"
                   "- **Date of birth** — Born on 16th April 2001.\n")
        self.assertIn("9,350 days", self.answer("How many days have I been alive?")["text"])
        files.append("memory", "My date of birth is 2002-04-19.", heading="## Saved on request")
        self.assertIn("date of birth", self.answer("How old am I?")["text"])

    def test_explicit_dates_leap_years_and_short_months(self):
        for born, today, expected in (
            (date(2000, 2, 29), date(2025, 2, 27), (299, 29)),
            (date(2000, 2, 29), date(2025, 2, 28), (300, 0)),
            (date(2025, 1, 31), date(2025, 3, 1), (1, 1)),
            (date(2024, 12, 31), date(2025, 1, 30), (0, 30)),
        ):
            with self.subTest(born=born, today=today):
                self.assertEqual(calendar_age(born, today), expected)
        for query in ("How many days old am I if I was born on April 16th, 2001?",
                      "I was born on April 16th, 2001. How old am I in days?"):
            self.assertIn("9,350 days", self.answer(query)["text"])
        self.assertIn("future", self.answer("How old am I if I was born on January 1, 2099?")["text"])
        self.chat("Remember that I was born on February 30, 2004.")
        self.assertIn("date of birth", self.answer("How old am I?")["text"])

    def test_exact_minutes_use_the_birth_instant_across_daylight_saving(self):
        self.chat("Remember that I was born on March 9, 2025.")
        with patch("nex.personal_age.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2025, 3, 9, 10, 30, tzinfo=UTC)
            self.assertIn("birth time", self.chat("How old am I in minutes?")["text"])
            for supplied in ("I was born at 1:30 am in America/Vancouver.", "At 1:30 am UTC-08:00"):
                self.assertIn("lived 60 minutes.", self.chat(supplied)["text"])
            self.assertIn("lived 3,600 seconds.", self.chat("And in seconds?")["text"])
        self.assertIn("about", self.answer("How old am I in minutes if I was born on "
                                           "November 3, 2024 at 1:30 am America/Vancouver?")["text"])


@unittest.skipUnless(importlib.util.find_spec("httpx"), "requires the live HTTP client")
class LiveAgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_age_question_requires_approval_before_using_birthdate(self):
        from nex.live.dialogue import Dialogue, Turn

        with tempfile.TemporaryDirectory() as root:
            dialogue = Dialogue(Store(root, 0, 2**20), "/unused", None)
            self.addAsyncCleanup(dialogue.close)
            history = [{"kind": "question", "text": "Remember that I was born on April 16th, 2001."},
                       {"kind": "answer", "text": "Saved.", "engine": "personal_context"},
                       {"kind": "question", "text": "How old am I?"},
                       {"kind": "answer", "text": "25.", "engine": "local_model"}]
            turn = Turn("example", "iphone", PersonalAgeTests.question, "en", PersonalAgeTests.zone,
                        PersonalAgeTests.asked)
            emit = Mock()
            dialogue.complete = AsyncMock(return_value=tools({"name": "age"}))
            with patch("nex.web_answers.lookup", side_effect=AssertionError("Unexpected web request")):
                result, queued = await dialogue.live_reply(turn, history, emit)
            self.assertFalse(queued)
            self.assertEqual(result["engine"], "private_request")
            self.assertNotIn("13,464,000", result["text"])
            self.assertEqual(result["text"], "")
            emit.assert_not_called()
