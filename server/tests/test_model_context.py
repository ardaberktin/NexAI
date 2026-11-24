import json
import unittest
from unittest.mock import Mock

import httpx

from nex.goal_writing import model_messages
from nex.live.dialogue import Dialogue
from nex.model_context import INPUT_TOKENS_PATH, ContextBudget, ContextLimit


class ModelContextTests(unittest.TestCase):
    def test_budget_compacts_history_then_excerpts_and_preserves_request_and_receipts(self):
        history = [{"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 100} for index in range(8)]
        receipt = {"status": "saved", "content": "y" * 450}
        question = "Full current request, including its last instruction."
        data = {"question": question, "history": history, "tools": {
            "save_memory": receipt, "web": [{"number": 2, "url": "https://example.org/", "excerpt": "z" * 650}],
        }, "recalled_chat": {"status": "found", "exchanges": [
            {"messages": [{"role": "assistant", "text": "r" * 650}]},
        ]}}
        options = {"messages": model_messages("Instructions", data), "max_tokens": 180}
        budget = ContextBudget(options, 1024)
        for candidate in budget.candidates():
            current = json.loads(candidate["messages"][1]["content"])
            if len(current["tools"]["web"][0]["excerpt"]) < 400:
                fitted = budget.fit(candidate, 777)
                break
            self.assertIsNone(budget.fit(candidate, 980))
        self.assertEqual(fitted["max_tokens"], 180)
        self.assertEqual(current["history"], [])
        self.assertEqual(current["question"], question)
        self.assertEqual(current["tools"]["save_memory"], receipt)
        self.assertEqual(current["tools"]["web"][0]["number"], 2)
        self.assertEqual(current["tools"]["web"][0]["url"], "https://example.org/")
        self.assertEqual(current["recalled_chat"]["status"], "found")
        self.assertEqual(current["recalled_chat"]["exchanges"][0]["messages"],
                         [{"role": "assistant", "text": "r" * 360 + "…"}])
        self.assertEqual(json.loads(options["messages"][1]["content"]), data)

    def test_large_reply_can_use_the_remaining_window_and_keep_its_sources(self):
        options = {"messages": model_messages("Instructions", {"goal": "Report", "evidence": {"status":"complete"}}),
                   "max_tokens": 2600}
        budget = ContextBudget(options, 4096)
        self.assertIsNone(budget.fit(options, 2096))
        reduced = budget.remaining()
        self.assertEqual(reduced, options | {"max_tokens": 1992})

    def test_oversize_request_fails_without_truncating_latest_message(self):
        options = {"messages": model_messages("Instructions", {"latest_message": "x" * 900}), "max_tokens": 60}
        budget = ContextBudget(options, 1024)
        self.assertIsNone(budget.fit(options, 990))
        with self.assertRaises(ContextLimit):
            budget.remaining()

    def test_older_backend_can_use_plain_completion_when_token_counting_is_unavailable(self):
        messages = model_messages("Instructions", {"question": "Hello"}, schema={"type": "object"})
        options = Dialogue.request(messages, 40)
        self.assertIn("response_format", options)
        fitted = ContextBudget(options, 4096).fit(options, None)
        self.assertNotIn("response_format", fitted)
        self.assertEqual(fitted["messages"], options["messages"])


class TokenTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_completion_counts_the_same_formatted_messages_it_sends(self):
        request = model_messages("instructions", {"question": "Merhaba"}, schema={"type": "object"})
        calls = []

        def handle(outgoing):
            calls.append((outgoing.url.path, json.loads(outgoing.content)))
            if outgoing.url.path == INPUT_TOKENS_PATH:
                return httpx.Response(200, json={"input_tokens": 430})
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        dialogue = Dialogue(Mock(), "/unused", None)
        await dialogue.local.aclose()
        dialogue.local = httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(dialogue.close)
        self.assertEqual(await dialogue.complete(dialogue.local, request, 260), "{}")
        self.assertEqual([path for path, _ in calls], [INPUT_TOKENS_PATH, "/v1/chat/completions"])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[1][1]["response_format"]["type"], "json_schema")
