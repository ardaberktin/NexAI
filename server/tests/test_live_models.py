import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from nex.store import Store


@unittest.skipUnless(importlib.util.find_spec("httpx"), "requires the live HTTP client")
class ModelSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nex.live.model_runtime import MODELS, ModelRuntime
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for _, name in MODELS.values():
            (root / name).touch()
        self.model = ModelRuntime(root, root, root / "selection")
        self.live = set()

        async def start(key):
            self.assertFalse(self.live)
            await asyncio.sleep(0)
            self.live.add(key)
            self.model.active = key
            self.model.process = type("Process", (), {"returncode": None})()

        async def stop():
            self.live.clear()
            self.model.active = self.model.process = None

        self.model.start = AsyncMock(side_effect=start)
        self.model.stop = AsyncMock(side_effect=stop)

    async def test_switches_serialize_and_idempotent_requests_share_the_process(self):
        await self.model.select("qwen")
        await asyncio.gather(self.model.select("gemma"), self.model.select("gemma"))
        self.assertEqual(self.live, {"gemma"})
        self.assertEqual(self.model.saved(), "gemma")
        self.assertEqual(self.model.start.await_count, 2)
        with self.assertRaises(ValueError):
            await self.model.select("unavailable")
        self.assertEqual(self.live, {"gemma"})

    async def test_failed_load_restores_last_working_selection(self):
        await self.model.select("qwen")
        start = self.model.start.side_effect

        async def fail_gemma(key):
            if key == "gemma":
                raise RuntimeError("model cannot start")
            await start(key)

        self.model.start.side_effect = fail_gemma
        with self.assertRaises(RuntimeError):
            await self.model.select("gemma")
        self.assertEqual(self.model.status()["active"], "qwen")
        self.assertEqual(self.model.saved(), "qwen")


@unittest.skipUnless(importlib.util.find_spec("httpx"), "requires the live HTTP client")
class ForegroundPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_standby_classifier_preempts_background_and_preserves_an_overlapping_voice_turn(self):
        from nex.live.dialogue import Dialogue
        with tempfile.TemporaryDirectory() as path:
            dialogue = Dialogue(Store(path, 0, 2**20), Path(path) / "model.sock", None)
            self.addAsyncCleanup(dialogue.close)
            started, release = asyncio.Event(), asyncio.Event()

            async def complete(*_args, **_kwargs):
                started.set()
                await release.wait()
                return "YES"

            dialogue.complete = complete
            decision = asyncio.create_task(dialogue.classify([]))
            await asyncio.wait_for(started.wait(), 2)
            self.assertFalse(dialogue.idle.is_set())
            dialogue.enter()
            release.set()
            self.assertEqual(await decision, "YES")
            self.assertTrue(dialogue.become_busy.is_set())
            dialogue.leave()
            self.assertTrue(dialogue.idle.is_set())
