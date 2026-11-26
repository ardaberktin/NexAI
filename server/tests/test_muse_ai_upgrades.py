"""Regression tests for Nex's Muse-class AI upgrades across runtime, agent loop, Sentinel, memory, browser, and skills."""

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from nex.active_memory import ActiveMemory
from nex.browser_tool import StructuredDOMParser, inspect_page
from nex.coding.answers import execute_python
from nex.coding.intent import PythonRequest
from nex.coding.sandbox import Run
from nex.conversation.agent import has_recoverable_gap, sanitize_goal_state
from nex.conversation.plan import Plan, Turn, validated
from nex.conversation.reply import compose, run
from nex.goal_runner import run_prompt
from nex.goals import Goals
from nex.http_api import chat_attachments
from nex.live.daybook_socket import daybook_socket
from nex.live.dialogue import Dialogue
from nex.live.model_runtime import ModelRuntime
from nex.mcp.permissions import Access
from nex.mcp.tool import Tool
from nex.model_context import ContextBudget, estimate_tokens
from nex.models import Engines
from nex.personal_files import PersonalFiles
from nex.sentinel import (
    PendingActions,
    TurnTaint,
    may_egress_query,
    parse_sentinel_verdict,
    sentinel_review_messages,
)
from nex.settings import Settings
from nex.skills_sandbox import SkillsManager
from nex.store import Store
from nex.web_transport import WebReadError
from nex.workspace_ideas import context as ideas_context


class RuntimeAndContextUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_multimodal_context_compaction_and_token_estimate(self):
        data = {
            "question": "What is in this receipt?",
            "history": [{"role": "user", "content": "a" * 200} for _ in range(6)],
            "active_memory": {"facts": [{"key": "note", "content": "b" * 500}]},
        }
        options = {
            "messages": [
                {"role": "system", "content": "Answer clearly."},
                {"role": "user", "content": [
                    {"type": "text", "text": json.dumps(data)},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]},
            ],
            "max_tokens": 200,
        }
        self.assertGreaterEqual(estimate_tokens(options), 550)
        budget = ContextBudget(options, 1200)
        candidates = list(budget.candidates())
        self.assertGreaterEqual(len(candidates), 3)
        compacted_parts = candidates[-1]["messages"][1]["content"]
        self.assertEqual(compacted_parts[1]["type"], "image_url")
        compacted_json = json.loads(compacted_parts[0]["text"])
        self.assertEqual(compacted_json["history"], [])
        self.assertEqual(compacted_json["question"], "What is in this receipt?")

    async def test_multi_model_discovery_ignores_legacy_qwen_and_finds_mmproj(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gemma-3-4b-it-qat-Q4_0.gguf").touch()
            (root / "qwen.gguf").touch()
            (root / "muse-glimmer-30b-Q4_K_M.gguf").touch()
            (root / "mmproj-muse-glimmer-30b-Q4_K_M.gguf").touch()
            runtime = ModelRuntime(root, root, root / "live-model")
            catalog = runtime.catalog()
            self.assertIn("gemma", catalog)
            self.assertIn("muse-glimmer", catalog)
            self.assertNotIn("qwen", catalog)
            self.assertIsNotNone(catalog["muse-glimmer"].projector)
            self.assertTrue(catalog["muse-glimmer"].reasoning)
            profiles = runtime._launch_profiles(catalog["muse-glimmer"])
            self.assertIn("-fa", profiles[0][3])
            self.assertIn("q8_0", profiles[0][3])
            self.assertIn("--mmproj", profiles[0][3])
            self.assertEqual(profiles[0][6], "auto")

    async def test_daybook_socket_supports_streaming_and_embeddings(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            sock = root / "daybook.sock"

            def handle(request):
                if request.url.path == "/v1/embeddings":
                    return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.5, 0.5, 0.5] * 4}]})
                if request.url.path == "/v1/chat/completions/input_tokens":
                    return httpx.Response(200, json={"input_tokens": 25})
                body = json.loads(request.content)
                if body.get("stream"):
                    sse = (
                        'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
                        'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
                        "data: [DONE]\n\n"
                    )
                    return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json={"choices": [{"message": {"content": "Hello world"}}]})

            dialogue = Dialogue(None, "/unused", None)
            await dialogue.local.aclose()
            dialogue.local = httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handle))
            self.addAsyncCleanup(dialogue.close)

            async with daybook_socket(sock, dialogue):
                settings = Settings(root)
                engines = Engines(settings, root / "runtime", live_socket=sock)
                chunks = []
                text = await asyncio.to_thread(
                    engines.generate_warm, [{"role": "user", "content": "Hi"}], 32,
                    on_text=chunks.append, thinking=True, reasoning_effort="medium",
                )
                self.assertEqual(text, "Hello world")
                self.assertEqual("".join(chunks), "Hello world")
                vectors = await asyncio.to_thread(engines.embed, ["hello"])
                self.assertEqual(len(vectors), 1)
                self.assertEqual(len(vectors[0]), 16)


class ActiveMemoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root, 0, 2**20)

    def test_unlimited_facts_and_bilingual_hybrid_recall(self):
        memory = ActiveMemory(self.store)
        for index in range(45):
            memory.upsert(f"Project {index}", f"Working on milestone {index} for sensor calibration",
                          category="project", sync_markdown=False)
        memory.upsert("Çay tercihi", "Sabahları demli siyah çay içerim", category="preference", sync_markdown=False)
        self.assertEqual(memory.count(), 46)
        en_hits = memory.recall("sensor calibration milestone 42", limit=5)
        self.assertTrue(any("milestone 42" in item["content"] for item in en_hits))
        tr_hits = memory.recall("siyah cay tercihi", limit=3)
        self.assertTrue(any("siyah çay" in item["content"] for item in tr_hits))

    def test_sync_from_markdown_and_ideas_enrichment(self):
        files = PersonalFiles(self.root)
        doc = files.read("memory")
        files.edit("memory", doc["revision"], doc["markdown"] + "\n## Learned from conversations\n- **Home lab** — Runs Excalibur on Linux\n")
        memory = ActiveMemory(self.store)
        self.assertGreaterEqual(memory.sync_from_markdown(), 1)
        bundle = memory.context_bundle("Excalibur Linux")
        self.assertIsNotNone(bundle)
        self.assertTrue(any("Excalibur" in item["content"] for item in bundle["facts"]))
        ideas_ctx = ideas_context(self.store)
        self.assertIn("active_memory", ideas_ctx)


class AgentLoopAndCodeRepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root, 0, 2**20)

    async def test_bounded_agent_loop_recovers_from_empty_archive_and_runs_followup(self):
        turn = Turn(str(uuid4()), "mac", "What did we decide about the greenhouse?", "en", "UTC",
                    started=datetime(2026, 11, 26, 12, 0, tzinfo=UTC), agent_loop=True)
        ActiveMemory(self.store).upsert("Greenhouse", "Ordered cedar framing for the greenhouse", sync_markdown=False)
        plan = Plan(({"name": "archive", "query": "nonexistent_term"},))
        calls = []

        async def generate(messages, tokens):
            calls.append(messages[0]["content"])
            if "Review the tool outcomes collected so far" in messages[0]["content"]:
                return json.dumps({"action": "call_tools", "tools": [{"name": "memory", "file": "memory", "query": "greenhouse"}]})
            return "You ordered cedar framing for the greenhouse."

        result = await run(self.store, turn, plan, generate, private=True)
        self.assertEqual(result["text"], "You ordered cedar framing for the greenhouse.")
        self.assertTrue(any("Review the tool outcomes" in prompt for prompt in calls))

    async def test_python_self_repair_supports_two_repair_attempts(self):
        attempts = [Run("error", "NameError: x"), Run("error", "ZeroDivisionError"), Run("ok", "42", 0)]
        generated = ["```python\nprint(x)\n```", "```python\nprint(1/0)\n```", "```python\nprint(42)\n```"]
        gen_iter = iter(generated)

        async def fake_generate(_messages):
            return next(gen_iter)

        with patch("nex.coding.answers.run_python", side_effect=attempts):
            result, source = await execute_python(PythonRequest("compute 42"), "en", fake_generate)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output, "42")
        self.assertIn("print(42)", source)

    async def test_multimodal_attachments_reach_compose_as_image_url_blocks(self):
        img = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
        self.assertEqual(chat_attachments({"attachments": [img]}), (img,))
        with self.assertRaises(ValueError):
            chat_attachments({"attachments": ["https://example.com/a.jpg"]})
        turn = Turn(str(uuid4()), "mac", "Describe this image", "en", "UTC",
                    started=datetime(2026, 11, 26, 12, 0, tzinfo=UTC), attachments=(img,))
        captured = []

        async def fake_generate(messages, _tokens):
            captured.append(messages)
            return "It shows a test image."

        text = await compose(turn, {}, fake_generate)
        self.assertEqual(text, "It shows a test image.")
        user_content = captured[0][-1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertEqual(user_content[1]["type"], "image_url")
        self.assertEqual(user_content[1]["image_url"]["url"], img)


class SentinelAndStatefulGoalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root, 0, 2**20)

    def test_sentinel_taint_egress_and_hitl_approval_flow(self):
        taint = TurnTaint()
        taint.mark("web")
        self.assertTrue(taint.tainted)
        self.assertFalse(may_egress_query("Summarize that page", "secret api key token", tainted=True))
        self.assertTrue(may_egress_query("Check https://example.org/status", "https://example.org/status", tainted=True))
        tool = Tool("send_order", "Submit an order", {"type": "object"}, False)
        msgs = sentinel_review_messages("Check prices then submit", "shop", tool, {"qty": 1}, Access.WRITE, tainted=True)
        payload = json.loads(msgs[1]["content"])
        self.assertTrue(payload["tainted"])
        self.assertNotIn("evidence", payload)
        self.assertEqual(parse_sentinel_verdict("WRITE", Access.WRITE, tainted=True), "APPROVE")

        actions = PendingActions(self.store)
        created = actions.create(
            source_type="goal", source_id="goal-1", goal_id="goal-1", tool_alias="mcp_shop_send",
            server_name="shop", tool_name="send_order", operation="write",
            arguments={"qty": 1}, summary="shop: send_order", tainted=True, reason="Tainted write",
        )
        self.assertEqual(created["status"], "pending")
        self.assertEqual(len(actions.list(status="pending")), 1)
        denied = actions.decide(created["id"], "deny")
        self.assertEqual(denied["status"], "denied")

    def test_stateful_goal_plan_state_persists_and_wakes(self):
        goals = Goals(self.store)
        goal_id = str(uuid4())
        anchor = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
        saved = goals.save(goal_id, {
            "prompt": "Track flight prices to Istanbul",
            "zone": "UTC", "frequency_minutes": 1440,
            "scheduled_at": "2026-11-27T12:00:00.000Z",
        }, anchor=anchor)
        initial_state = {"phase": "researching", "milestones": [{"id": "m1", "title": "Check baseline", "status": "in_progress"}], "scratchpad": []}
        Goals.save_plan_state(self.store, goal_id, initial_state)
        detail = goals.detail(goal_id)
        self.assertEqual(detail["plan_state"]["phase"], "researching")
        self.assertTrue(Goals.wake(self.store, goal_id, anchor=anchor))

        outputs = iter([
            json.dumps({"sources": ["none"]}),
            "Checked baseline fares; direct flights start around $780.",
            json.dumps({
                "phase": "monitoring",
                "milestones": [{"id": "m1", "title": "Check baseline", "status": "done", "note": "$780 baseline"}],
                "scratchpad": ["Baseline direct fare $780"],
            }),
        ])
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        result = run_prompt(self.store, row, anchor, lambda _m, _t: next(outputs), lambda _m: "YES")
        self.assertIn("$780", result.text)
        updated = goals.detail(goal_id)
        self.assertEqual(updated["plan_state"]["phase"], "monitoring")
        self.assertEqual(updated["plan_state"]["milestones"][0]["status"], "done")
        self.assertEqual(updated["plan_state"]["scratchpad"], ["Baseline direct fare $780"])
        self.assertTrue(saved["id"] == goal_id)


class BrowserAndSkillsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root, 0, 2**20)

    def test_browser_structured_dom_parser_and_ssrf_guard(self):
        html = (
            "<html><head><title>Nex Status</title>"
            '<script type="application/ld+json">{"@type":"WebPage","name":"Status"}</script></head>'
            "<body><h1>System Operational</h1><p>All services are green. Latency is 12ms.</p>"
            '<a href="https://example.org/docs">Docs</a><button aria-label="Refresh">Go</button></body></html>'
        )
        fake_resp = SimpleNamespace(content=html)
        with patch("nex.browser_tool._render_dom_chromium", return_value=None), \
             patch("nex.browser_tool.fetch", return_value=fake_resp):
            result = inspect_page("https://example.org/status", action="open")
        self.assertEqual(result["title"], "Nex Status")
        self.assertTrue(result["tainted"])
        self.assertIn("System Operational", result["headings"])
        self.assertEqual(result["links"][0]["url"], "https://example.org/docs")
        self.assertTrue(any(item["tag"] == "button" for item in result["interactive"]))
        with self.assertRaises(WebReadError):
            inspect_page("http://127.0.0.1:8766/")
        plan = validated([{"name": "browser", "url": "https://example.org/status", "action": "open"}])
        self.assertEqual(plan.tools[0]["name"], "browser")
        self.assertFalse(plan.private)
        self.assertTrue(has_recoverable_gap({"web": {"unavailable": "timeout"}}))
        self.assertIsNotNone(StructuredDOMParser("https://example.org"))

    async def test_skills_manager_requires_approval_before_execution(self):
        manager = SkillsManager(self.store)
        skill = manager.propose("unit_convert", "Convert Celsius to Fahrenheit", "print('ok')")
        self.assertFalse(skill["approved"])
        blocked = await manager.execute("unit_convert", {"c": 20})
        self.assertEqual(blocked["status"], "approval_required")
        approved = manager.approve(skill["id"])
        self.assertTrue(approved["approved"])
        with patch("nex.skills_sandbox.run_persistent_python",
                   new=AsyncMock(return_value=Run("ok", "68.0", 0))):
            ran = await manager.execute("unit_convert", {"c": 20})
        self.assertEqual(ran["status"], "ok")
        self.assertEqual(ran["output"], "68.0")
        self.assertEqual(manager.get("unit_convert")["run_count"], 1)
        self.assertEqual(manager.delete(skill["id"]), {"deleted": True})

    def test_sanitize_goal_state_bounds_and_preserves_valid_fields(self):
        state = sanitize_goal_state(json.dumps({
            "phase": "monitoring",
            "milestones": [{"title": "Check price", "status": "done", "note": "Verified"}],
            "scratchpad": ["Observed $400"],
        }))
        self.assertEqual(state["phase"], "monitoring")
        self.assertEqual(state["milestones"][0]["status"], "done")
        self.assertEqual(state["scratchpad"], ["Observed $400"])


if __name__ == "__main__":
    unittest.main()
