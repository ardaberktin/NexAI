import asyncio
import importlib.util
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from nex.conversation_recall import time_key
from nex.live_memory import LiveMemory
from nex.store import Store
from nex.voice_chime import CHIME_PCM
from nex.workspace_content import chat_history


class SharedVoiceHistoryTests(unittest.TestCase):
    def test_automatic_opening_and_recorded_followup_have_stable_chat_pairs(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path, 0, 2**20)
            memory = LiveMemory(path)
            opened = datetime.now(UTC)
            result = {"text": "Hello", "language": "en", "sources": [], "engine": "local_model"}
            seed = SimpleNamespace(id="first", device="iphone", started=opened, text="Hey there",
                                   archived=False, journal=None)
            memory.save_turn_reply(seed, result)
            memory.save_turn_reply(seed, result)
            self.assertEqual([r["id"] for r in chat_history(store)], ["q-first", "a-first"])
            seed.journal, seed.id = "daybook", "scoped"
            memory.save_turn_reply(seed, result)
            self.assertEqual(len(chat_history(store)), 2)
            followup = SimpleNamespace(id=str(uuid4()), device="iphone", started=opened + timedelta(seconds=2),
                                       text="I'm good too, thanks", archived=True, journal=None)
            # The phone's clock can place its audio slightly before the server's turn timestamp.
            store.save_recording(followup.id, "iphone", time_key(opened), 1.2, "voice", False, "en",
                                 b"audio", live_handled=True)
            with store.connect() as db:
                db.execute("UPDATE recordings SET state='ready' WHERE id=?", (followup.id,))
            memory.save_turn_reply(followup, result)
            memory.save_turn_reply(followup, result)
            self.assertEqual([r["id"] for r in chat_history(store)], [
                "q-first", "a-first", "q-" + followup.id, "a-" + followup.id,
            ])


@unittest.skipUnless(importlib.util.find_spec("pipecat"), "requires the live speech virtual environment")
class LiveFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nex.live.session import Session
        self.socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock())
        self.dialogue = Mock()
        self.dialogue.live_reply = AsyncMock(return_value=(
            {"text": "Glad to hear it.", "language": "en", "engine": "local_model", "sources": []}, False))
        speech = Mock()
        speech.listen.return_value.vad.num_frames_required.return_value = 512
        speech.speak.side_effect = lambda *_: iter([bytes(4800)])
        self.verifier = SimpleNamespace(check=AsyncMock(side_effect=lambda audio, lang, text: text))
        self.session = Session(self.socket, "iphone", "en", "UTC", False, speech, self.dialogue, self.verifier)

    async def finish(self, text):
        await self.session.finish_turn("followup", datetime.now(UTC), 32_000, b"audio", text)
        await self.session.playback
        return self.dialogue.live_reply.call_args.args[0]

    async def test_automatic_conversation_accepts_plain_answers_and_single_word_turns(self):
        await self.session.begin_with_question("How are you doing?")
        await self.session.playback
        for phrase in ("I'm good too, thanks", "Yes, please", "No", "Tuesday"):
            with self.subTest(phrase=phrase):
                self.assertEqual((await self.finish(phrase)).text, phrase)
        self.dialogue.classify.assert_not_called()
        self.assertFalse(any(call.args[0]["kind"] == "not_addressed" for call in self.socket.send_json.call_args_list))

    async def test_release_followup_uses_public_topic_even_after_assistant_offers_to_check(self):
        await self.session.begin_with_question("When is GTA Six coming out?")
        await self.session.playback
        self.session.history[-1]["text"] = "Would you like me to check a reliable source for you?"
        turn = await self.finish("Yes please")
        self.assertEqual(turn.text, "Yes please")
        history = self.dialogue.live_reply.await_args.args[1]
        self.assertIn("GTA Six", history[0]["text"])
        self.assertIn("check a reliable source", history[1]["text"])

    async def test_public_platform_question_and_spoken_challenge_take_priority_over_screen(self):
        self.session.screen_replies = True
        await self.session.begin_with_question("Where can I play GTA Six?")
        await self.session.playback
        turn = await self.finish("No you're wrong. You can't play it on PC right now")
        self.assertIn("PC", turn.text)
        self.assertEqual(self.dialogue.live_reply.await_args.args[1][0]["text"], "Where can I play GTA Six?")
        self.assertNotIn("screen_question", [c.args[0]["kind"] for c in self.socket.send_json.call_args_list])

    async def test_python_uses_the_server_during_screen_sharing(self):
        self.session.screen_replies = True
        turn = await self.finish("Use Python to count the vowels in Excalibur.")
        self.assertEqual(turn.text, "Use Python to count the vowels in Excalibur.")
        self.assertNotIn("screen_question", [call.args[0]["kind"] for call in self.socket.send_json.call_args_list])

    async def test_implicit_computation_can_preempt_a_screen_request(self):
        self.session.request_screen_answer = AsyncMock()
        text = "The fare was forty dollars for five of us. Split it evenly."
        turn = await self.finish(text)
        self.assertEqual(turn.text, text)
        self.assertEqual(self.session.history[-2]["text"], text)
        self.dialogue.live_reply.assert_awaited_once()
        self.session.request_screen_answer.assert_not_awaited()

    async def test_age_followup_uses_the_server_calculator_while_screen_sharing(self):
        self.session.screen_replies = True
        self.session.request_screen_answer = AsyncMock(return_value=None)
        await self.session.begin_with_question("How old am I?")
        await self.session.playback
        turn = await self.finish("How many months and days and minutes?")
        self.assertEqual(turn.text, "How many months and days and minutes?")
        self.session.request_screen_answer.assert_not_awaited()

    async def test_timeout_starts_after_the_speaker_finishes(self):
        await self.session.begin_with_question("How are you?")
        await self.session.playback
        self.session.last_turn = monotonic() - 60
        self.session.audible_until = monotonic() - 15
        self.assertFalse(self.session.timed_out())
        self.session.audible_until = monotonic() - 46
        self.assertTrue(self.session.timed_out())

    async def test_direct_speech_chimes_on_the_first_words_and_keeps_the_whole_turn(self):
        from nex.live.session import Session
        from pipecat.audio.vad.vad_analyzer import VADState

        speech = self.session.speech
        session = Session(self.socket, "mac", "en", "UTC", False, speech, self.dialogue, self.verifier, local_chime=True)
        listener = session.listening
        listener.vad.analyze_audio = AsyncMock(return_value=VADState.SPEAKING)
        listener.feed.side_effect = ["", "How", "How are you"]
        listener.finish.return_value = "How are you?"
        frame = bytes(1024)
        await session.input.receive(frame)
        self.assertEqual([call.args[0]["kind"] for call in self.socket.send_json.await_args_list], ["turn"])
        await session.input.receive(frame)
        self.assertEqual(self.socket.send_json.await_args.args[0]["kind"], "chime")
        self.verifier.check.assert_not_awaited()
        self.dialogue.live_reply.assert_not_awaited()
        await session.input.receive(frame)
        await session.input.flush()
        await session.playback
        kinds = [call.args[0]["kind"] for call in self.socket.send_json.await_args_list]
        self.assertEqual(kinds, ["turn", "chime", "turn_end", "speaker", "reply"])
        self.verifier.check.assert_awaited_once_with(frame * 3, "en", "How are you?")

    async def test_ticketed_opening_skips_duplicate_cue_and_only_accepted_followups_chime(self):
        from nex.live.session import Session

        gate = SimpleNamespace(resolve=AsyncMock(side_effect=["", "Yes, please"]))
        session = Session(self.socket, "mac", "en", "UTC", False, self.session.speech, self.dialogue,
                          self.verifier, gate=gate, local_chime=True, opening_chimed=True)
        session.voices = SimpleNamespace(identify=Mock(return_value="recognized"))
        await session.begin_with_question("How are you?", audio=b"opening audio")
        await session.playback
        session.voices.identify.assert_called_once_with(b"opening audio")
        self.assertEqual(self.dialogue.live_reply.await_args.args[0].speaker, "recognized")
        self.assertIsNone(session.input.on_words)
        self.assertNotIn("chime", [call.args[0]["kind"] for call in self.socket.send_json.await_args_list])
        self.socket.send_json.reset_mock()
        await session.start_turn("room", 0)
        await session.finish_turn("room", datetime.now(UTC), 2048, b"room", "Pass the glass.")
        self.socket.send_json.assert_awaited_once_with({"kind": "progress", "end": 2048})
        await session.start_turn("followup", 2048)
        await session.finish_turn("followup", datetime.now(UTC), 4096, b"followup", "Yes, please")
        await session.playback
        chimes = [call.args[0] for call in self.socket.send_json.await_args_list if call.args[0]["kind"] == "chime"]
        self.assertEqual(chimes, [{"kind": "chime", "id": "followup"}])
        self.socket.send_bytes.assert_not_awaited()

    async def test_every_accepted_turn_acknowledges_before_generation_and_can_be_interrupted(self):
        entered = asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.dialogue.live_reply.side_effect = slow
        await self.session.begin_with_question("Explain the phases of the Moon.")
        await entered.wait()
        self.socket.send_bytes.assert_awaited_once_with(CHIME_PCM)
        await self.session.start_turn("next", 100)
        self.assertTrue(any(c.args[0]["kind"] == "stop_audio" for c in self.socket.send_json.await_args_list))
        self.dialogue.live_reply.side_effect = None
        await self.finish("What did I just ask you?")
        self.assertEqual([call.args[0] for call in self.socket.send_bytes.await_args_list], [CHIME_PCM] * 2)
        history = self.dialogue.live_reply.await_args.args[1]
        self.assertEqual(history, [])
        await self.session.finish_turn("silence", datetime.now(UTC), 65_000, b"", "")
        self.assertEqual(self.socket.send_bytes.await_count, 2)

    async def test_suspect_followup_uses_the_second_recognizer_after_acknowledgment(self):
        self.session.history = [
            {"kind": "question", "text": "What did I just ask?", "started": time_key(datetime.now(UTC))},
            {"kind": "answer", "text": "Earlier…", "engine": "archive_search", "recall_cursor": {"before": "past"}},
        ]
        async def recover(*_):
            self.socket.send_bytes.assert_awaited_once_with(CHIME_PCM)
            return "Yeah, what before that?"

        self.verifier.recover = AsyncMock(side_effect=recover)
        turn = await self.finish("Gewap before that")
        self.assertEqual(turn.text, "Yeah, what before that?")
        self.assertEqual(self.dialogue.memory.save_result.call_args.kwargs["transcript"], turn.text)

    async def test_an_uncertain_word_after_i_is_checked_against_the_actual_audio(self):
        from nex.conversation_intent import earlier_question
        for original, recovered in (("I mint before that", "I meant before that"),
                                    ("I worked before that", "I worked before that")):
            self.session.history = [
                {"kind": "question", "text": "What did I just ask?"},
                {"kind": "answer", "text": "Earlier…", "recall_cursor": {"before": "past"}},
            ]
            self.verifier.recover = AsyncMock(return_value=recovered)
            turn = await self.finish(original)
            self.verifier.recover.assert_awaited_once()
            self.assertEqual(turn.text, recovered)
            self.assertEqual(earlier_question(turn.text, allow_noisy=True), original != recovered)

    async def test_a_generation_failure_returns_status_without_speaking(self):
        self.dialogue.live_reply.side_effect = ValueError("Model returned an empty reply")
        await self.session.begin_with_question("Explain the seasons.")
        await self.session.playback
        self.assertEqual(self.socket.send_bytes.await_args_list[0].args, (CHIME_PCM,))
        self.session.speech.speak.assert_not_called()
        reply = self.socket.send_json.await_args.args[0]["reply"]
        self.assertEqual((reply["engine"], reply["text"]), ("model_unavailable", ""))
