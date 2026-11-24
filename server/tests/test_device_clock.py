import json
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from conversation_fixtures import model
from nex.conversation.clock import DeviceClock
from nex.live.device_clock import DeviceClockTurn
from nex.live.public_reply import public_reply
from nex.privacy.public import answer, chat
from nex.store import Store

HOME = "America/Vancouver"
AWAY = "Asia/Kathmandu"
DEVICE_TIME = datetime.fromisoformat("2026-11-23T12:45:30+05:45")
READING = {
    "unix_seconds": int(DEVICE_TIME.timestamp()),
    "utc_offset_seconds": 20_700,
    "time_zone": AWAY,
}


def model_clock(call):
    data = json.loads(call.args[0][1]["content"])
    return data["local_time"], data["local_time_display"], data["local_time_zone"]


class ChatClockTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self.enterContext(tempfile.TemporaryDirectory()), 0, 2**20)
        self.generate = model([], compose="Your device shows 12:45 PM.")
        self.assistant = Mock(store=self.store, generate_conversation=self.generate)

    def test_chat_passes_the_asking_device_clock_to_each_model_call(self):
        result = chat(self.assistant, str(uuid4()), "iphone", "How late is it where I am?", "en", HOME,
                      device_clock=READING)
        expected = "2026-11-23T12:45+05:45", "12:45 PM", AWAY
        self.assertEqual([model_clock(call) for call in self.generate.call_args_list], [expected] * 3)
        self.assertEqual(result["engine"], "local_model")
        self.assertEqual(result["text"], "Your device shows 12:45 PM.")

    def test_queued_voice_uses_the_original_recording_time_in_the_recorders_zone(self):
        asked_at = datetime.fromisoformat("2026-11-23T07:00:30+00:00")
        output = Mock()
        result = answer(self.assistant, self.store.privacy, str(uuid4()), "watch", "What's the time?", "en", AWAY,
                        asked_at=asked_at, spoken=True, emit=output)
        self.assertEqual(model_clock(self.generate.call_args), ("2026-11-23T12:45+05:45", "12:45 PM", AWAY))
        output.assert_called_once_with(result["text"], "en")

    def test_device_offset_takes_precedence_over_the_servers_time_zone_rules(self):
        sample = READING | {"time_zone": HOME, "utc_offset_seconds": -7 * 3600}
        clock = DeviceClock.read(sample)
        self.assertEqual(clock.local_time().isoformat(), "2026-11-23T00:00:30-07:00")


class LiveClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_voice_waits_for_a_clock_sample_from_its_own_turn(self):
        identifier = str(uuid4())
        pending = DeviceClockTurn(identifier)
        message = {"device_clock": READING}
        pending.receive(message | {"id": "another-turn"})
        self.assertFalse(pending.reply.done())
        pending.receive(message | {"id": identifier})

        voice = SimpleNamespace(id=identifier, device="iphone", text="How late is it where I am?",
                                language="en", zone=HOME, started=datetime.fromisoformat("2026-11-23T06:00:00+00:00"),
                                journal=None, coordinates=None)
        generate = model([], compose="Your device shows 12:45 PM.")
        dialogue = SimpleNamespace(complete=AsyncMock(side_effect=lambda _, messages, *args, **kwargs: generate(messages)),
                                   local=object(), memory=None)
        read_clock = AsyncMock(side_effect=lambda _: pending.reply.result())
        output = Mock()
        result = await public_reply(dialogue, voice, [], output, read_clock=read_clock)

        read_clock.assert_awaited_once_with(identifier)
        expected = "2026-11-23T12:45+05:45", "12:45 PM", AWAY
        self.assertEqual([model_clock(call) for call in generate.call_args_list[1:]], [expected, expected])
        self.assertEqual(result["text"], "Your device shows 12:45 PM.")
        output.assert_called_once_with(result["text"], "en")
