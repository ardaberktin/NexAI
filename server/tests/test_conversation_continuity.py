import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from conversation_fixtures import model
from nex.assistant import Assistant
from nex.conversation_history import recent_dialogue
from nex.conversation_recall import recent_question_reply
from nex.live_memory import LiveMemory
from nex.store import Store
from nex.voice_lookup import voice_lookup_query
from nex.web_grounding import dated_followup
from nex.worker import Worker
from nex.workspace_content import chat_history


class ConversationContinuityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(folder.name, 0, 2**20)
        self.memory = LiveMemory(folder.name)
        self.engine = Mock()
        self.engine.managed_live.return_value = False
        self.assistant = Assistant(self.store, self.engine, SimpleNamespace(read=dict))
        self.now = datetime.now(UTC)

    def exchange(self, question, answer, seconds, *, engine="local_model", sources=(), **metadata):
        turn = SimpleNamespace(id=str(uuid4()), device="iphone", text=question, archived=True, journal=None,
                               started=self.now - timedelta(seconds=seconds))
        self.memory.save_turn_reply(turn, {"text": answer, "language": "en", "sources": list(sources),
                                          "engine": engine, **metadata})
        return "q-" + turn.id

    def ask(self, question):
        tool = {"name": "past_questions", "count": 1}
        if question in ("Gewap before that", "And the one before that?", "I meant before that", "Before that?",
                        "And before that?"):
            tool["before"] = "last_result"
        if "yesterday" in question:
            tool["start_date"] = (self.now.date() - timedelta(days=1)).isoformat()
        self.assistant.generate_conversation = model([tool], compose=lambda data: "\n".join(
            data["tools"]["past_questions"]["questions"]) or "I couldn’t find any earlier questions.")
        result = self.assistant.answer(question, "en")
        self.exchange(question, result["text"], -0.001, engine=result["engine"], sources=result["sources"],
                      recall_cursor=result.get("recall_cursor"))
        self.now = datetime.now(UTC)
        return result

    def test_navigate_past_a_repeated_question_after_reconnecting_before_audio_upload(self):
        first = self.exchange("Who designed the telescope?", "Its designer was Mira.", 60)
        second = self.exchange("When does the observatory open?", "In December.", 50)
        self.exchange("How many days is that?", "Twelve days.", 40)
        last = self.exchange("How many days is that?", "Twelve days.", 30)
        self.assertEqual(self.ask("What did I just ask you?")["sources"], [last])
        self.assertEqual(self.ask("Gewap before that")["sources"], [second])
        self.assertEqual(self.ask("And the one before that?")["sources"], [first])
        exhausted = self.ask("I meant before that")
        self.assertEqual(exhausted["sources"], [])
        self.assertIn("couldn’t find", exhausted["text"])
        self.engine.generate.assert_not_called()

    def test_old_recall_answers_and_a_misunderstood_correction_keep_the_reference(self):
        topic = self.exchange("When do the doors open?", "At six.", 65)
        days = self.exchange("How many days is that?", "Five.", 55)
        self.exchange("What did I just ask you?", "Here’s what you asked earlier.", 45,
                      engine="archive_search", sources=[days])
        self.exchange("Gewap before that", "Could you clarify that word?", 35)
        self.assertEqual(self.ask("I meant before that")["sources"], [topic])
        self.exchange("Explain how fossils form", "Remains are buried in sediment.", -5)
        later = self.now + timedelta(seconds=6)
        self.assertIsNone(recent_question_reply(self.store, "What happened before that?", "en", None,
                                                asked_at=later, history=recent_dialogue(self.store, later)))
        self.assertIsNone(recent_question_reply(self.store, "Before that?", "en", None, asked_at=later,
                                                history=recent_dialogue(self.store, later)))

    def test_date_scope_survives_navigation_at_the_start_of_a_day(self):
        midnight = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        for when, label in ((midnight - timedelta(days=2), "Older day"),
                            (midnight - timedelta(hours=6), "First question yesterday"),
                            (midnight - timedelta(hours=2), "Last question yesterday")):
            self.exchange(label, "Done.", (self.now - when).total_seconds())
        result = self.ask("What was my last question yesterday?")
        self.assertIn("Last question yesterday", result["text"])
        self.assertIn("First question yesterday", self.ask("Before that?")["text"])
        self.assertEqual(self.ask("And before that?")["sources"], [])

    def test_countdown_uses_the_preceding_cited_date_and_local_day(self):
        self.now = datetime(2026, 11, 19, 2, tzinfo=UTC)
        self.exchange("When does the observatory open?", "November 19, 2026. [1]", 6,
                      engine="web_lookup", sources=["public-page"])
        with patch("nex.web_answers.lookup") as lookup:
            answer = dated_followup("How many days is that?", recent_dialogue(self.store, self.now),
                                    self.now, "America/Vancouver", "en")
        self.assertEqual((answer["text"], answer["sources"]), ("That’s 1 day away.", ["public-page"]))
        history = recent_dialogue(self.store, self.now) + [
            {"kind": "question", "text": "How many days is that?"}, {"kind": "answer", **answer},
        ]
        self.assertEqual(dated_followup("How many days?", history, self.now, "America/Vancouver", "en"), answer)
        lookup.assert_not_called()
        self.engine.generate.assert_not_called()
        self.assertIsNone(voice_lookup_query("How many days is that?", [
            {"kind": "question", "text": "My holiday lasts three weeks."}, {"kind": "answer", "text": "Three weeks."},
        ]))
        self.assertEqual(chat_history(self.store)[-1]["sources"], ["public-page"])

    def test_general_followups_use_ordered_roles_and_only_recent_ordinary_exchanges(self):
        self.exchange("Old private chat", "Old answer", 3600)
        first = self.exchange("The blue suitcase is Cedar.", "Understood.", 80)
        self.exchange("Rename it Amber.", "The blue suitcase is now Amber.", 70)
        scoped = self.exchange("A selected Daybook question", "Scoped details", 60)
        with self.store.connect() as db:
            db.execute("UPDATE memory SET engine='daybook' WHERE id=?", (scoped,))
        self.exchange("Later than the queued question", "Future details", -60)
        self.engine.generate.return_value = None
        self.engine.generate_warm = model([], compose="Amber.")
        spoken = Mock()
        answer = self.assistant.answer("What did we just talk about?", "en", spoken=True, asked_at=self.now,
                                       on_speech=spoken)
        self.assertEqual(answer["text"], "Amber.")
        spoken.assert_called_once_with("Amber.", "en")
        messages = self.engine.generate_warm.call_args.args[0]
        import json
        data = json.loads(messages[1]["content"])
        self.assertEqual([(m["role"], m["content"]) for m in data["history"]] + [("user", data["question"])], [
            ("user", "The blue suitcase is Cedar."), ("assistant", "Understood."),
            ("user", "Rename it Amber."), ("assistant", "The blue suitcase is now Amber."),
            ("user", "What did we just talk about?"),
        ])
        self.assertEqual(recent_dialogue(self.store, self.now)[0]["id"], first)

    def test_recorded_voice_acknowledges_once_and_speaks_the_answer_if_generation_did_not_stream(self):
        worker = Worker(self.store, self.engine, self.assistant, self.assistant.settings)
        target = {"id": "recorded", "device": "iphone", "started": self.now.isoformat(), "time_zone": "UTC"}
        question = SimpleNamespace(text="How much is two plus three?", target=target)
        result = {"text": "Five.", "language": "en", "sources": [], "engine": "local_model"}
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                def answer(*args, streamed=streamed, **kwargs):
                    self.assertEqual(worker.voice_streams.turns["recorded"][1], [{"kind": "chime"}])
                    if streamed:
                        kwargs["emit"]("Five.", "en")
                    return result
                with patch("nex.worker.public_answer", side_effect=answer):
                    self.assertEqual(worker.answer_spoken(question, "en", "recorded"), result | {"speaker": "unverified"})
                events = list(worker.voice_streams.events("recorded"))
                self.assertEqual([e["kind"] for e in events], ["chime", "speech", "complete"])
                self.assertEqual(events[1]["text"], "Five.")
