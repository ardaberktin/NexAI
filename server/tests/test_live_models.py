import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nex.store import Store


@unittest.skipUnless(importlib.util.find_spec("httpx"), "requires the live HTTP client")
class ModelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nex.live.model_runtime import ModelRuntime
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.model = ModelRuntime(root, root, root / "live-model")
        self.model.weights.touch()
        (root / "qwen.gguf").touch()
        self.start = AsyncMock(side_effect=self.start_gemma)

    async def start_gemma(self):
        await asyncio.sleep(0)
        self.model.device = "Vulkan"
        process = SimpleNamespace(returncode=None)
        process.terminate = lambda: setattr(process, "returncode", 0)
        process.wait = AsyncMock(return_value=0)
        self.model.process = process

    async def test_legacy_selection_starts_gemma_and_rejects_qwen(self):
        self.model.marker.write_text("qwen\n")
        self.model.start = self.start
        async with self.model.running():
            self.assertEqual(self.model.marker.read_text(), "gemma\n")
            status = self.model.status()
            self.assertEqual(status["active"], "gemma")
            self.assertEqual([item["id"] for item in status["models"]], ["gemma"])
            with self.assertRaises(ValueError):
                await self.model.select("qwen")
        self.start.assert_awaited_once()

    async def test_idempotent_requests_share_the_process(self):
        self.model.start = self.start
        first, second = await asyncio.gather(self.model.select("gemma"), self.model.select("gemma"))
        self.addAsyncCleanup(self.model.stop)
        self.assertEqual(first, second)
        self.assertEqual(first["active"], "gemma")
        self.start.assert_awaited_once()

    async def test_missing_gemma_fails_even_with_qwen_installed(self):
        self.model.weights.unlink()
        with self.assertRaisesRegex(RuntimeError, "Missing Excalibur model weights"):
            async with self.model.running():
                self.fail("The managed runtime must report a failed startup.")
        self.assertIsNone(self.model.status()["active"])
        self.assertFalse(self.model.status()["models"][0]["available"])
        self.assertEqual(self.model.marker.read_text(), "gemma\n")

    async def test_gemma_start_failure_is_reported_without_a_second_model_attempt(self):
        self.model.start = AsyncMock(side_effect=RuntimeError("Gemma could not start"))
        with self.assertRaisesRegex(RuntimeError, "Gemma could not start"):
            async with self.model.running():
                self.fail("The managed runtime must report a failed startup.")
        self.model.start.assert_awaited_once()
        self.assertIsNone(self.model.status()["active"])


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
