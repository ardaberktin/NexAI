import unittest

from nex.live.control import control_payload
from nex.live.screen import ScreenTurn


class ScreenTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_matching_answer_finishes_the_turn(self):
        turn = ScreenTurn("current")
        turn.receive(control_payload('{"kind":"screen_result","id":"previous","text":"Old answer"}'))
        self.assertFalse(turn.reply.done())
        turn.receive(control_payload('{"kind":"screen_result","id":"current","text":"A shared window"}'))
        self.assertEqual(await turn.wait(), "A shared window")
        turn.receive(control_payload('{"kind":"screen_result","id":"current","text":"Duplicate"}'))
        self.assertEqual(turn.reply.result(), "A shared window")

    async def test_unshared_screen_uses_ordinary_voice_and_late_answers_are_ignored(self):
        turn = ScreenTurn("current")
        turn.receive(control_payload('{"kind":"screen_result","id":"current"}'))
        self.assertIsNone(await turn.wait())
        timed_out = ScreenTurn("expired")
        with self.assertRaises(TimeoutError):
            await timed_out.wait(seconds=0)
        timed_out.receive({"kind": "screen_result", "id": "expired", "text": "Arrived too late"})

    async def test_rejects_invalid_or_excessive_messages(self):
        turn = ScreenTurn("current")
        with self.assertRaises(ValueError):
            control_payload("unexpected")
        with self.assertRaises(ValueError):
            control_payload("a" * 8193)
        with self.assertRaises(ValueError):
            turn.receive({"kind": "screen_result", "id": "current", "text": "a" * 1201})
        self.assertFalse(turn.reply.done())
