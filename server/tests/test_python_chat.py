import importlib.util
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from conversation_fixtures import model, tools
from nex.assistant import Assistant
from nex.coding.answers import generation_messages, repair_prompt
from nex.coding.function_examples import (
    FUNCTION_INSTRUCTIONS,
    TEST_RUNNER,
    has_examples,
)
from nex.coding.intent import ENGINE, PythonRequest, python_request
from nex.coding.sandbox import Run
from nex.http_api import chat_message
from nex.settings import Settings
from nex.store import Store
from nex.voice_lookup import lookup_query
from nex.wake import addressed_question


class PythonChatTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.engine = Mock()
        self.engine.generate = model([{"name":"python"}], code="print(42)")
        self.assistant = Assistant(Store(folder.name, 0, 2**20), self.engine, Settings(folder.name))

    def ask(self, text, **kwargs):
        return self.assistant.chat(text, "en", "mac", str(uuid4()), **kwargs)

    def test_the_model_receives_example_guidance_for_each_request(self):
        for task in ("Could you write a short function that recognizes even numbers?",
                     "Write a function without tests.", "Dinner costs 86.40 for six. What does everyone owe?"):
            with self.subTest(task=task):
                messages = generation_messages(PythonRequest(task), "en")
                self.assertIn(FUNCTION_INSTRUCTIONS, messages[0]["content"])
                self.assertEqual(messages[-1]["content"], task)
        self.assertTrue(has_examples('def f():\n    """\n    >>> f()\n    42\n    """\n    return 42'))
        self.assertFalse(has_examples('print(">>> f()")'))

    def test_natural_math_and_python_use_the_sandbox_including_while_screen_sharing(self):
        with patch("nex.coding.answers.run_python", return_value=Run("ok", "42")) as execute:
            args = ("What's the median of 41, 42 and 90?", "en", "mac", str(uuid4()))
            self.engine.generate = model([{"name":"python"}], code="```python\nfrom statistics import median\nprint(median([41,42,90]))\n```")
            answer = self.assistant.chat(*args, lookup_only=True)
            self.assertEqual(answer["engine"], ENGINE)
            self.assertTrue(answer["text"].startswith("42"))
            self.assertEqual(self.assistant.chat(*args), answer)
            self.assertEqual(self.assistant.chat(*args, lookup_only=True), answer)
            execute.assert_awaited_once()
            for question in ("What is 17.5% of 240?", "What's seventeen point five percent of 240?",
                             "Hey Nex, what is six times seven?", "6 × (3+4)"):
                with self.subTest(question=question):
                    self.assertEqual(self.ask(question)["text"].splitlines()[0], "42")
                    self.assertIsNone(lookup_query(question))
            self.assertEqual(execute.await_count, 5)

    def test_multiline_program_is_run_as_supplied_and_errors_are_preserved(self):
        for inline in ("Run Python: `print(7)`", "Can you run `print(7)` in Python?"):
            self.assertEqual(python_request(inline).source, "print(7)")
        code = "for x in range(2):\n    print(x)\nraise ValueError('example')"
        with patch("nex.coding.answers.run_python", return_value=Run("error", "0\n1\nValueError: example", 1)) as run:
            answer = self.ask("Run this Python:\n```python\n" + code + "\n```")
        run.assert_awaited_once_with(code)
        self.assertIn("0\n1\nValueError: example", answer["text"])
        self.assertIn("status 1", answer["text"])
        self.assertEqual(self.engine.generate.call_count, 2)

    def test_long_scripts_are_accepted_and_their_contents_never_become_personal_requests(self):
        source = "# Ask what am I doing today and what's in my inbox?\n" * 85 + "print(42)"
        text = "Run Python:\n" + source
        self.assertEqual(chat_message({"text": text}), text)
        for invalid in ({"text": "word " * 600}, {"text": text, "daybook_day": "2026-01-01"},
                        {"text": "Run Python:\n#" + "é" * 6100}):
            with self.assertRaises(ValueError):
                chat_message(invalid)
        with patch("nex.coding.answers.run_python", return_value=Run("ok", "42")) as run:
            self.assertEqual(self.ask(text, lookup_only=True)["text"], "42")
            run.assert_awaited_once_with(source)
        with self.assistant.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM personal_events").fetchone()[0], 0)

    def test_generation_repairs_once_and_preserves_the_verified_code_and_output(self):
        programs = iter(["print(missing)", "```python\nprint(40 + 2)\n```"])
        self.engine.generate = model([{"name":"python"}], code=lambda _: next(programs))
        with patch("nex.coding.answers.run_python", side_effect=[Run("error", "NameError", 1), Run("ok", "42")]) as run:
            emit = Mock()
            result = self.assistant.answer("Write a Python script to add forty and two.", "en", spoken=True, on_speech=emit)
        self.assertEqual([call.args[0] for call in run.await_args_list], ["print(missing)", "print(40 + 2)"])
        self.assertEqual(result["text"], "42\n\n```python\nprint(40 + 2)\n```")
        emit.assert_called_once_with("42", "en")
        self.assertIn("NameError", self.engine.generate.call_args_list[-2].args[0][-1]["content"])

    def test_failed_self_test_keeps_the_final_assertion_for_repair(self):
        failure = "AssertionError: is_even(3) returned False, expected True"
        message = repair_prompt(Run("error", "earlier output\n" * 100 + failure, 1))
        self.assertTrue(message.endswith(failure))
        self.assertIn("Keep the failing case", message)

    def test_model_unavailability_and_sandbox_failure_have_no_fabricated_result(self):
        self.engine.generate = Mock(return_value=None)
        self.engine.generate_warm.return_value = None
        with patch("nex.coding.answers.run_python", return_value=Run("unavailable")) as run:
            result = self.assistant.answer("Use Python to group these names by initial.", "en")
            self.assertEqual((result["engine"], result["text"]), ("model_unavailable", ""))
            run.assert_not_awaited()
            self.engine.generate = model([{"name":"python"}])
            answer = self.ask("Run Python: print(2 + 2)")
            self.assertIn("unavailable", answer["text"])
            run.assert_awaited_once_with("print(2 + 2)")

    def test_only_a_direct_request_and_its_own_recent_python_context_drive_execution(self):
        task = "Use Python to compute the median of 4, 8 and 12."
        history = [{"kind": "question", "text": task}, {"kind": "answer", "text": "8", "engine": ENGINE}]
        self.assertEqual(python_request("Now double that", history).previous, (task, "8"))
        self.assertIsNone(python_request("Now double that"))
        self.assertIsNone(python_request("Use Python to sort three names", history).previous)
        self.assertEqual(addressed_question("Hey Nex, use Python to count these words"),
                         "use Python to count these words")
        for question in ("List the first 10 primes.", "If I save $20 a week for 12 weeks, how much will I have?"):
            self.assertIsNotNone(python_request(question))
            self.assertIsNone(lookup_query(question))
        for text in ("How do I install Python?", "What's on my calendar today?", "Search the web for Python",
                     "Write a Python function without running it", "Explain this code:\n```python\nprint(7)\n```",
                     "```python\nprint(7)\n```\nDon't run this. Explain it."):
            with self.subTest(text=text):
                self.assertIsNone(python_request(text, history))


@unittest.skipUnless(importlib.util.find_spec("httpx"), "requires the live HTTP client")
class PythonVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_spoken_request_runs_python_and_reads_only_the_result(self):
        from nex.live.dialogue import Dialogue, Turn

        with tempfile.TemporaryDirectory() as folder:
            dialogue = Dialogue(Store(folder, 0, 2**20), "/unused", None)
            self.addAsyncCleanup(dialogue.close)
            dialogue.complete = AsyncMock(side_effect=["CLEAR", tools({"name":"python"}), "print(sum(range(10)))", "45"])
            turn = Turn("id", "iphone", "Use Python to sum the integers from zero to nine.",
                        "en", "UTC", datetime.now(UTC))
            emit = Mock()
            with patch("nex.coding.answers.run_python", return_value=Run("ok", "45")) as run:
                result, queued = await dialogue.live_reply(turn, [], emit)
            self.assertFalse(queued)
            self.assertEqual(result["engine"], ENGINE)
            run.assert_awaited_once_with("print(sum(range(10)))")
            emit.assert_called_once_with("45", "en")

    @unittest.skipUnless(shutil.which("bwrap"), "requires the Linux Python sandbox")
    async def test_function_request_recovers_from_stdin_and_executes_its_own_examples(self):
        from nex.coding.sandbox import run_python
        from nex.live.dialogue import Dialogue, Turn

        question = "Write a function that checks whether an integer is even."
        function = "def is_even(n):\n    return n % 2 == 0\n"
        interactive = function + "print(is_even(int(input())))"
        tested = ('def is_even(n):\n    """\n    >>> is_even(8)\n    True\n'
                  '    >>> is_even(3)\n    False\n    """\n    return n % 2 == 0')
        with tempfile.TemporaryDirectory() as folder:
            dialogue = Dialogue(Store(folder, 0, 2**20), "/unused", None)
            self.addAsyncCleanup(dialogue.close)
            dialogue.complete = AsyncMock(side_effect=["CLEAR", tools({"name":"python"}), interactive, tested, "2 function tests passed."])
            turn = Turn("id", "iphone", question, "en", "UTC", datetime.now(UTC))
            emit = Mock()
            with patch("nex.coding.answers.run_python", wraps=run_python) as execute:
                reply, queued = await dialogue.live_reply(turn, [], emit)

        self.assertFalse(queued)
        self.assertEqual(reply["engine"], ENGINE)
        self.assertEqual([call.args[0] for call in execute.await_args_list],
                         [interactive, tested + TEST_RUNNER])
        repair = dialogue.complete.await_args_list[-2].args[1]
        self.assertEqual(repair[1]["content"], question)
        self.assertIn("EOFError", repair[-1]["content"])
        self.assertIn("no stdin", repair[-1]["content"])
        self.assertIn("test cases", repair[-1]["content"])
        self.assertIn(tested, reply["text"])
        emit.assert_called_once_with("2 function tests passed.", "en")

    @unittest.skipUnless(shutil.which("bwrap"), "requires the Linux Python sandbox")
    async def test_missing_or_incorrect_examples_cannot_report_a_passing_run(self):
        from nex.coding.sandbox import run_python

        for program, diagnostic in (
            ("def double(n):\n    return 2 * n\nprint('Tests passed!')", "No function tests ran."),
            ('def double(n):\n    """\n    >>> double(3)\n    6\n    """\n    return n', "Got:\n    3"),
        ):
            with self.subTest(diagnostic=diagnostic):
                run = await run_python(program + TEST_RUNNER)
                self.assertEqual(run.status, "error")
                self.assertIn(diagnostic, run.output)
