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
    async def test_public_stream_and_private_handoff_have_separate_inputs_and_outputs(self):
        import httpx

        from nex.live.dialogue import Dialogue, Turn
        from nex.privacy.requests import inbox

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(directory.name, 0, 2**20)
        dialogue = Dialogue(store, "/unused", None)
        await dialogue.local.aclose()
        sends = []
        generate = model([], compose="Hello. Welcome.")

        def stream(request):
            sends.append(json.loads(request.content))
            content = generate(sends[-1]["messages"])
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        dialogue.local = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(stream))
        self.addAsyncCleanup(dialogue.close)
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
            handoff, _ = await dialogue.live_reply(private, [], speech)
            self.assertEqual(handoff["engine"], "private_request")
            self.assertEqual(len(sends), lookups + 2)
            self.assertEqual(handoff["text"], "")
            speech.assert_not_called()
            self.assertIsNone(inbox(store.privacy)["items"][0]["answer"])
