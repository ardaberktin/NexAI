import asyncio
import importlib.util
import io
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

LIVE_DEPENDENCIES = ("fastapi", "httpx", "soundfile", "numpy")


@unittest.skipUnless(all(importlib.util.find_spec(dep) for dep in LIVE_DEPENDENCIES), "needs the live voice environment")
class WatchReplyVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI

        from nex.live.watch_reply_voice import install_watch_reply_voice

        app = FastAPI()
        self.reply = {"text": "A short answer.", "public_voice": True, "language": "en"}
        self.row = {"device": "watch", "mode": "voice", "state": "ready", "reply": self.reply}
        self.memory = Mock()
        self.memory.recording.return_value = self.row
        app.state.dialogue = SimpleNamespace(memory=self.memory)
        self.speech = app.state.speech = Mock()
        self.speech.speak.return_value = [b"\x00\x10" * 12000]
        install_watch_reply_voice(app, lambda r: r.headers.get("x-test-device"))
        self.path = "/v2/watch/recordings/" + str(uuid4()) + "/voice"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                        headers={"x-test-device": "iphone"})
        self.addAsyncCleanup(self.client.aclose)

    async def test_renders_complete_mp3_with_the_matching_live_voice(self):
        import soundfile as sf

        for language in ("en", "tr"):
            self.reply["language"] = language
            response = await self.client.get(self.path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "audio/mpeg")
            self.speech.speak.assert_called_with(self.reply["text"], language)
            with sf.SoundFile(io.BytesIO(response.content)) as clip:
                self.assertEqual((clip.format, clip.samplerate, clip.channels, clip.frames), ("MP3", 24000, 1, 12000))

    async def test_audio_uses_the_same_privacy_boundary_as_the_receipt(self):
        self.assertEqual((await self.client.get(self.path, headers={"x-test-device": "mac"})).status_code, 403)
        self.memory.recording.assert_not_called()
        for change in ({"device": "iphone"}, {"mode": "ambient"}, {"state": "processing"},
                       {"reply": {"text": "private", "engine": "archive"}}, {"reply": None}):
            self.memory.recording.return_value = self.row | change
            self.assertEqual((await self.client.get(self.path)).status_code, 404)
        self.speech.speak.assert_not_called()

    async def test_private_recording_can_play_only_the_approval_notice(self):
        self.reply.update(engine="private_request", text="Previously stored reply text")
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        self.speech.speak.assert_not_called()
        self.reply.update(approval_notice="Approve the pending request in Nex.")
        self.assertEqual((await self.client.get(self.path)).status_code, 200)
        self.speech.speak.assert_called_once_with(self.reply["approval_notice"], "en")

    async def test_failed_render_and_busy_voice_allow_local_fallback(self):
        entered, release = Event(), Event()

        def held_render(*_args):
            entered.set()
            release.wait(timeout=3)
            return [b"\x00\x10" * 12000]

        self.speech.speak.side_effect = held_render
        first = asyncio.create_task(self.client.get(self.path))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertEqual((await self.client.get(self.path)).status_code, 429)
        finally:
            release.set()
        self.assertEqual((await first).status_code, 200)
        self.speech.speak.side_effect = None
        self.speech.speak.return_value = []
        self.assertEqual((await self.client.get(self.path)).status_code, 503)
        self.reply["text"] = "x" * 1801
        self.assertEqual((await self.client.get(self.path)).status_code, 422)
