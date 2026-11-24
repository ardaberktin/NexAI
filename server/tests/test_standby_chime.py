import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from nex.live.attention import Attention


@unittest.skipUnless(importlib.util.find_spec("pipecat") and importlib.util.find_spec("fastapi"),
                     "requires the live speech virtual environment")
class StandbyChimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from nex.live.standby import Standby
        from pipecat.audio.vad.vad_analyzer import VADState

        self.socket = Mock(send_json=AsyncMock())
        speech = Mock()
        self.listener = speech.listen.return_value
        self.listener.vad.num_frames_required.return_value = 512
        self.listener.vad.analyze_audio = AsyncMock(return_value=VADState.SPEAKING)
        self.attention = Attention(speech, Mock(classify=AsyncMock(return_value="NO")), Mock())
        self.active = asyncio.Lock()
        state = SimpleNamespace(speech=speech, attention=self.attention, active=self.active)
        self.standby = Standby(state, self.socket, "mac", "en", wake_chime=True)

    def events(self):
        return [call.args[0] for call in self.socket.send_json.await_args_list]

    async def test_named_prefix_chimes_before_endpoint_and_preserves_all_samples(self):
        question = "what do I have to do today?"
        self.listener.feed.side_effect = ["Hey", "Hey Nex", "Hey Nex what"]
        self.listener.finish.return_value = "Hey Nex, " + question
        frame = bytes(1024)
        await self.standby.input.receive(frame)
        self.assertEqual(self.events(), [])
        await self.standby.input.receive(frame)
        wake, = self.events()
        self.assertEqual(wake["kind"], "wake_chime")
        self.assertIsNone(self.attention.tickets.claim("mac", wake["wake_token"]))
        await self.standby.input.receive(frame)
        await self.standby.input.flush()
        self.assertEqual(self.events()[1:], [{"kind": "decision", "ticket": wake["wake_token"], "named": True}])
        claimed = self.attention.tickets.claim("mac", wake["wake_token"])
        self.assertEqual((claimed.text, claimed.audio), (question, frame * 3))

    async def test_a_bare_wake_and_the_question_after_the_pause_share_one_cue(self):
        await self.standby.finished("wake", None, 1000, b"name", "Hey Nex.")
        wake, decision = self.events()
        self.assertIsNone(decision["ticket"])
        await self.standby.finished("question", None, 3000, b"full question", "Which route should I take?")
        self.assertEqual(self.events()[2:], [{"kind": "decision", "ticket": wake["wake_token"], "named": True}])
        self.assertEqual(self.attention.tickets.claim("mac", wake["wake_token"]).audio, b"full question")

    async def test_ambient_words_never_chime_before_the_attention_decision(self):
        for phrase, accepted in [("Can you bring me the glass?", False), ("What's the weather outside?", True)]:
            with self.subTest(phrase=phrase):
                self.attention.dialogue.classify.return_value = "YES" if accepted else "NO"
                self.socket.send_json.reset_mock()
                self.listener.feed.return_value = self.listener.finish.return_value = phrase
                await self.standby.input.receive(bytes(1024))
                self.assertEqual(self.events(), [])
                await self.standby.input.flush()
                decision, = self.events()
                self.assertEqual((decision["kind"], bool(decision["ticket"]), decision["named"]),
                                 ("decision", accepted, False))

    async def test_a_named_greeting_can_chime_while_a_partial_soundalike_stays_quiet(self):
        frame = bytes(1024)
        self.listener.feed.side_effect = ["Nex", "Next week", "Next weekend"]
        self.listener.finish.return_value = "Next weekend"
        self.attention.verifier.recover = AsyncMock(return_value="Next weekend")
        await self.standby.input.receive(frame * 3)
        self.assertEqual(self.events(), [])
        await self.standby.input.flush()
        self.assertIsNone(self.events()[0]["ticket"])
        self.listener.feed.side_effect = ["Hey Next"]
        await self.standby.input.receive(frame)
        self.assertEqual(self.events()[-1]["kind"], "wake_chime")

    async def test_two_devices_share_pickup_and_a_declined_question_can_move_to_the_other(self):
        tickets = self.attention.tickets
        for declined in (False, True):
            with self.subTest(declined=declined):
                first = await tickets.offer("mac", "What time is it?", False, b"mac", self.active, None)
                other = asyncio.create_task(tickets.offer("iphone", "What time is it?", False, b"phone", self.active, None))
                await asyncio.sleep(0)
                self.assertFalse(other.done())
                if declined:
                    tickets.decline("mac", first)
                    second = await other
                    self.assertEqual(tickets.claim("iphone", second).audio, b"phone")
                else:
                    self.assertEqual(tickets.claim("mac", first).audio, b"mac")
                    self.assertIsNone(await other)

    async def test_a_disconnected_standby_releases_its_unfinished_wake(self):
        await self.standby.heard_name("partial")
        token = self.events()[0]["wake_token"]
        self.listener.prime_vad = AsyncMock(side_effect=TimeoutError)
        self.listener.smart.cleanup = self.listener.vad.cleanup = AsyncMock()
        with self.assertRaises(TimeoutError):
            await self.standby.run()
        self.assertFalse(self.attention.tickets.waking("mac", token))
        self.assertIsNotNone(self.attention.tickets.reserve_wake("iphone", self.active))
