import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conversation_fixtures import model
from nex.assistant import Assistant
from nex.conversation_recall import recent_question_reply, time_key
from nex.live_memory import LiveMemory
from nex.recall_dates import recall_dates
from nex.store import Store


class ConversationRecallTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(folder.name, 0, 1_048_576)
        self.assistant = Assistant(self.store, None, None)
        self.assistant.generate_conversation = model(
            lambda data: [{"name": "past_questions", "count": 2 if "before" in data["question"] else 1}],
            quotes=lambda data: [item["number"] for item in data["candidates"] if "quantum" in item["text"]]
            or [data["candidates"][0]["number"]],
        )
        self.asked_at = datetime(2026, 11, 18, 8, 45, tzinfo=UTC)

    def voice(self, text, minutes, *, reply="I answered the question."):
        identifier = str(uuid4())
        started = time_key(self.asked_at - timedelta(minutes=minutes))
        self.store.save_recording(identifier, "mac", started, 1.5, "voice", False, "en", b"test", live_handled=True)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state='ready',transcript=?,reply=? WHERE id=?",
                       (text, reply, identifier))
            self.store.add_memory(db, identifier, started, "recording", "mac", text)
        return identifier

    def typed(self, kind, text, *, minutes):
        identifier = str(uuid4())
        started = time_key(self.asked_at - timedelta(minutes=minutes))
        with self.store.connect() as db:
            self.store.add_memory(db, identifier, started, kind, "iphone", text)
        return identifier

    def test_returns_the_recent_quantum_questions_instead_of_older_assistant_greetings(self):
        first = self.voice("What are the latest advancements on quantum mechanics", 5)
        second = self.voice("Yeah, tell me more about quantum entanglement", 4)
        self.voice("How are you doing today?", 3)
        self.voice("Nèx.", 2)
        self.voice("What did I ask you before", 1)
        self.typed("answer", "I'm doing well, thank you for asking. How can I help you today?", minutes=60)

        query = "What did I ask you before?"
        for spoken in (False, True):
            answer = self.assistant.answer(query, "en", asked_at=self.asked_at, spoken=spoken)
            self.assertEqual(answer["sources"], [second, first])
            self.assertIn("quantum mechanics", answer["text"])
            self.assertIn("quantum entanglement", answer["text"])
            self.assertNotIn("doing well", answer["text"])

    def test_uses_live_history_while_the_recording_uploads(self):
        self.voice("What did we learn about gravity?", 8)
        current = str(uuid4())
        answer = recent_question_reply(
            self.store, "What did I just ask?", "en", None, asked_at=self.asked_at, exclude=current,
            history=[{"kind": "question", "id": str(uuid4()), "text": "Show me the original transcript",
                      "started": time_key(self.asked_at - timedelta(seconds=5))},
                     {"kind": "question", "id": current, "text": "What did I just ask?",
                      "started": time_key(self.asked_at - timedelta(seconds=1))}],
        )
        self.assertIn("Show me the original transcript", answer["text"])
        self.assertNotIn("gravity", answer["text"])
        self.assertEqual(answer["sources"], [])

    def test_model_selected_count_routes_unfamiliar_wording_to_exact_archived_quotes(self):
        requests = ["Explain wave functions", "Compare the two methods", "Show me the experiment notes"]
        sources = [self.voice(text, 5 - index) for index, text in enumerate(requests)]
        self.assistant.generate_conversation = model([{"name": "past_questions", "count": 3}])
        answer = self.assistant.answer("Could you quote my last three requests?", "en", asked_at=self.asked_at)
        self.assertEqual(answer["sources"], list(reversed(sources)))
        self.assertTrue(all(text in answer["text"] for text in requests))

    def test_saved_automatic_question_is_available_in_the_next_session(self):
        live = LiveMemory(self.store.root)
        identifier = str(uuid4())
        live.save_spoken_question(identifier, "mac", self.asked_at - timedelta(minutes=2), "Explain wave functions")
        answer = self.assistant.answer("What was my last question?", "en", asked_at=self.asked_at)
        self.assertIn("Explain wave functions", answer["text"])
        self.assertEqual(answer["sources"], ["q-" + identifier])

    def test_filters_by_date_and_reports_no_results_when_only_assistant_replies_exist(self):
        self.typed("question", "What changed in today’s release?", minutes=5)
        self.typed("answer", "What can I help you with?", minutes=60 * 24)
        query = "What did I ask you yesterday?"
        scope = recall_dates(query, asked_at=self.asked_at)
        answer = recent_question_reply(self.store, query, "en", scope, asked_at=self.asked_at)
        self.assertEqual(answer["sources"], [])
        self.assertEqual(answer["text"], "I couldn’t find an earlier question from you.")
