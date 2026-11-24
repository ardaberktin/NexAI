import importlib.util
import tempfile
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from conversation_fixtures import model, tools, unlock
from nex.assistant import Assistant
from nex.coding.intent import ENGINE
from nex.coding.sandbox import Run
from nex.conversation.plan import validated
from nex.settings import Settings
from nex.store import Store
from nex.worker import Worker

BILL = "The bill came to $86.40. There are six of us. What does everyone owe?"
FOLLOWUP = "A seventh person just joined us. What's each share now?"


def history(answer, engine, *, question=BILL, sources=()):
    return [{"kind": "question", "text": question},
            {"kind": "answer", "engine": engine, "text": answer, "sources": list(sources)}]


class ToolSelectionTests(unittest.TestCase):
    def test_only_a_valid_plan_can_start_generation(self):
        for invalid in ("RUN", "SKIP", None, [{"name": "python", "source": "print(42)"}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validated(invalid)
        self.assertEqual(validated([]).tools, ())
        self.assertEqual(validated([{"name": "python", "query": BILL}]).tools[0]["query"], BILL)

    def test_private_data_and_sandbox_computation_require_unlock_together(self):
        self.assertFalse(validated([{"name": "python"}]).private)
        self.assertTrue(validated([{"name": "python"}, {"name": "gmail"}]).private)

    def test_opt_out_and_tool_exclusions_reach_the_planning_model(self):
        from nex.conversation.sync import make_turn
        for question in ("Split the fare without using Python.", "Don't use tools. Just explain.",
                         "Tell me a joke without searching the web."):
            self.assertEqual(make_turn("id", "watch", question, "en", "UTC").prompt()["question"], question)


class ChatToolSelectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.engine = Mock()
        self.assistant = Assistant(Store(folder.name, 0, 2**20), self.engine, Settings(folder.name))

    def test_preflight_runs_once_and_the_followup_uses_that_bill(self):
        from nex.privacy import public
        from nex.privacy.chat_history import ChatFollowups
        cache = ChatFollowups()
        session = unlock(self.assistant.store)
        def choose(data):
            if data["question"] == FOLLOWUP:
                self.assertIn(BILL, str(data["history"]))
                return [{"name":"python","query":"86.40 / 7"}]
            return [{"name":"python","query":"86.40 / 6"}]
        self.engine.generate = model(choose, code=lambda messages: "print(" + messages[-1]["content"] + ")")
        def ask(identifier, question, preflight=False):
            return public.chat(self.assistant, identifier, "mac", question, "en", "UTC", lookup_only=preflight,
                               session=session, followups=cache)
        identifier = str(uuid4())
        with patch("nex.coding.answers.run_python", side_effect=[Run("ok","14.40"), Run("ok","12.34")]) as run:
            original = ask(identifier, BILL, True)
            self.assertEqual(ask(identifier, BILL), original)
            follow = ask(str(uuid4()), FOLLOWUP)
        self.assertEqual([call.args[0] for call in run.await_args_list], ["print(86.40 / 6)", "print(86.40 / 7)"])
        self.assertTrue(original["text"].startswith("14.40"))
        self.assertTrue(follow["text"].startswith("12.34"))

    def test_missing_or_negative_decision_keeps_the_ordinary_lookup_path(self):
        with patch("nex.web_answers.lookup") as web, patch("nex.coding.answers.run_python") as python:
            for invalid in ("SKIP", "", None, "RUN: print(2)"):
                self.engine.generate.return_value = invalid
                self.assertEqual(self.assistant.answer("Who won the 2018 World Cup?", "en")["engine"], "model_unavailable")
            web.assert_not_called()
            python.assert_not_called()

    def test_a_new_question_with_its_own_inputs_can_be_reconsidered_without_the_previous_topic(self):
        question = "Which day of the week was November 1, 2020?"
        self.engine.generate = model([{"name":"python","query":question}], code="print('Sunday')")
        with patch("nex.assistant.recent_dialogue", return_value=history("$14.40", ENGINE)), patch(
            "nex.coding.answers.run_python", return_value=Run("ok","Sunday")
        ):
            result = self.assistant.answer(question, "en")
        self.assertTrue(result["text"].startswith("Sunday"))
        self.assertNotIn(BILL, str(self.engine.generate.call_args_list[-2]))

    def test_recorded_voice_computes_from_natural_speech_and_uses_local_date(self):
        question = "How many days from today until December 1?"
        turn = SimpleNamespace(text=question, target={"id": "clip", "time_zone": "America/Vancouver",
                              "started": "2026-11-22T03:00:00+00:00", "device": "iphone"})
        worker = Worker(self.assistant.store, self.engine, self.assistant, self.assistant.settings)
        worker.voice_streams = Mock()
        self.engine.classify.return_value = "RUN"
        self.engine.generate = model([{"name":"python"}], code="from datetime import date\nprint((date(2026,12,1)-date(2026,11,21)).days)")
        with patch("nex.coding.answers.run_python", return_value=Run("ok", "10")):
            self.assertEqual(worker.answer_spoken(turn, "en", None)["engine"], ENGINE)
        system = self.engine.generate.call_args_list[-2].args[0][0]["content"]
        self.assertIn("2026-11-21", system)
        self.assertEqual(worker.voice_streams.publish.call_args.args, ("clip", "10", "en"))


@unittest.skipUnless(importlib.util.find_spec("httpx"), "requires the live HTTP client")
class LiveToolSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_voice_checks_once_and_executes_before_a_speculative_web_lookup(self):
        from nex.live.dialogue import Dialogue, Turn
        with tempfile.TemporaryDirectory() as folder:
            dialogue = Dialogue(Store(folder, 0, 2**20), "/unused", None)
            self.addAsyncCleanup(dialogue.close)
            turn = Turn("id","iphone",BILL,"en","UTC",datetime.now(UTC))
            dialogue.complete = AsyncMock(side_effect=["CLEAR", tools({"name":"python"}),"print(86.40 / 6)", "$14.40"])
            emit = Mock()
            with patch("nex.web_answers.lookup") as web, patch("nex.coding.answers.run_python", return_value=Run("ok","$14.40")) as compute:
                result, queued = await dialogue.live_reply(turn, [], emit)
            web.assert_not_called()
            compute.assert_awaited_once()
            self.assertEqual(result["engine"], ENGINE)
            self.assertFalse(queued)
            emit.assert_called_once_with("$14.40","en")
