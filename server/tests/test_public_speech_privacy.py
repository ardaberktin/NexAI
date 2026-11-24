import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

from conversation_fixtures import model
from nex.store import Store


@unittest.skipUnless(importlib.util.find_spec("httpx"), "needs live voice httpx")
class PublicSpeechPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def dialogue_with_model(self, generate):
        import httpx

        from nex.live.dialogue import Dialogue

        store = Store(self.enterContext(tempfile.TemporaryDirectory()), 0, 2**20)
        dialogue = Dialogue(store, "/unused", None)
        await dialogue.local.aclose()
        sends = []

        def stream(request):
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"input_tokens": 50})
            sends.append(json.loads(request.content))
            content = generate(sends[-1]["messages"])
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        dialogue.local = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(stream))
        self.addAsyncCleanup(dialogue.close)
        return dialogue, sends

    async def test_public_stream_and_private_handoff_have_separate_inputs_and_outputs(self):
        from nex.live.dialogue import Turn
        from nex.privacy.requests import inbox

        notice = "Open Nex to approve your pending request."

        def speak_pending(status):
            self.assertEqual(status, {"status": "pending_approval", "next_action": "open_nex_to_approve",
                                      "language": "en"})
            return notice

        generate = model([], compose="Hello. Welcome.", approval_notice=speak_pending)
        dialogue, sends = await self.dialogue_with_model(generate)
        store = dialogue.memory
        with store.connect() as db:
            store.add_memory(db, "test-owner-secret", datetime.now(UTC).isoformat(), "note", "mac", "secret fixture")
        speech = Mock()
        historic = [{"kind": "question", "text": "secret fixture"}]
        for device, speaker in [("iphone", "recognized"), ("mac", "unverified")]:
            general = Turn(str(uuid4()), device, "Tell me a poem.", "en", "UTC", datetime.now(UTC), speaker=speaker)
            reply, _ = await dialogue.live_reply(general, historic, speech)
            self.assertEqual(reply["text"], "Hello. Welcome.")
            self.assertNotIn("secret fixture", json.dumps(sends[-1]))
            lookups = len(sends)
            private = Turn(str(uuid4()), device, "What is my home address?", "en", "UTC", datetime.now(UTC), speaker=speaker)
            speech.reset_mock()
            handoff, _ = await dialogue.live_reply(private, historic, speech)
            self.assertEqual(handoff["engine"], "private_request")
            self.assertEqual(len(sends), lookups + 3)
            self.assertEqual(handoff["text"], "")
            self.assertEqual(handoff["approval_notice"], notice)
            speech.assert_called_once_with(notice, "en")
            self.assertIsNone(inbox(store.privacy)["items"][0]["answer"])

    async def test_notice_model_failure_preserves_pending_request_and_app_status(self):
        import httpx

        from nex.live.dialogue import Turn
        from nex.privacy.requests import inbox

        def unavailable(_status):
            raise httpx.ConnectError("model unavailable")

        dialogue, _ = await self.dialogue_with_model(model([], approval_notice=unavailable))
        turn = Turn(str(uuid4()), "iphone", "What is my home address?", "en", "UTC", datetime.now(UTC))
        speech = Mock()
        handoff, _ = await dialogue.live_reply(turn, [], speech)
        self.assertEqual(handoff["engine"], "private_request")
        self.assertNotIn("approval_notice", handoff)
        speech.assert_not_called()
        pending, = inbox(dialogue.memory.privacy)["items"]
        self.assertEqual((pending["id"], pending["state"], pending["answer"]), (turn.id, "pending", None))

    async def test_full_inbox_does_not_announce_an_unqueued_request(self):
        from nex.live.dialogue import Turn
        from nex.privacy.requests import queue

        def unexpected(_status):
            self.fail("The model must only announce a committed private request")

        dialogue, _ = await self.dialogue_with_model(model([], approval_notice=unexpected))
        for _ in range(24):
            queue(dialogue.memory.privacy, str(uuid4()), "iphone", "Pending request", "en", "UTC")
        turn = Turn(str(uuid4()), "iphone", "What is my home address?", "en", "UTC", datetime.now(UTC))
        speech = Mock()
        handoff, _ = await dialogue.live_reply(turn, [], speech)
        self.assertEqual(handoff["engine"], "private_queue_full")
        speech.assert_not_called()
