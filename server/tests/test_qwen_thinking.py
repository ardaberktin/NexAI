import unittest
from unittest.mock import AsyncMock

from nex.live.dialogue import QWEN_THINKING_TOKENS, Dialogue


class QwenThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.active = "qwen-3.5-4b"
        self.dialogue = Dialogue(None, "/unused", "/unused-mac", local_model_id=lambda: self.active)
        self.addAsyncCleanup(self.dialogue.close)
        self.dialogue.budget_completion = AsyncMock(side_effect=lambda _, options: options)

    async def options(self, client, **overrides):
        return await self.dialogue.completion_options(client, [], 8, temperature=0, reasoning_effort=None,
                                                      thinking=False, **overrides)

    async def test_managed_qwen_reserves_answer_tokens_for_chat_and_streams(self):
        for stream in (True, False):
            options = await self.options(self.dialogue.local, stream=stream)
            self.assertTrue(options["chat_template_kwargs"]["enable_thinking"])
            self.assertEqual(options["reasoning_budget_tokens"], QWEN_THINKING_TOKENS)
            self.assertGreaterEqual(options["max_tokens"] - options["reasoning_budget_tokens"], 8)
            self.assertEqual(options["stream"], stream)
        self.dialogue.budget_completion.side_effect = lambda _, options: {**options, "max_tokens": 40}
        fitted = await self.options(self.dialogue.local)
        self.assertEqual(fitted["reasoning_budget_tokens"], 24)

    async def test_standby_mac_and_model_switch_keep_their_own_options(self):
        immediate = await self.options(self.dialogue.local, immediate=True)
        mac = await self.options(self.dialogue.mac)
        self.active = "gemma"
        gemma = await self.options(self.dialogue.local)
        for options in (immediate, mac, gemma):
            self.assertFalse(options["chat_template_kwargs"]["enable_thinking"])
            self.assertNotIn("reasoning_budget_tokens", options)
            self.assertEqual(options["max_tokens"], 8)
