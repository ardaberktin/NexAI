import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nex.coding.arithmetic import arithmetic_source
from nex.coding.sandbox import MAX_OUTPUT, MAX_SOURCE, run_python


class SandboxAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_isolation_never_runs_the_host_interpreter(self):
        with (patch("nex.coding.sandbox.asyncio.create_subprocess_exec", side_effect=FileNotFoundError) as launch,
              self.assertLogs("nex.coding.sandbox", level="WARNING")):
            self.assertEqual((await run_python("print(123)")).status, "unavailable")
        self.assertEqual(launch.await_count, 1)
        self.assertEqual(launch.await_args.args[0], "/usr/bin/bwrap")

    async def test_source_budget_counts_bytes_before_launch(self):
        with patch("nex.coding.sandbox.asyncio.create_subprocess_exec") as launch:
            self.assertEqual((await run_python("é" * MAX_SOURCE)).status, "source_limit")
            launch.assert_not_awaited()


@unittest.skipUnless(sys.platform == "linux" and Path("/usr/bin/bwrap").is_file(), "requires Linux Bubblewrap")
class LinuxSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_arithmetic_and_standard_library_run_in_real_python(self):
        for question, expected in (
            ("What is 17.5% of 240?", "42"), ("twenty-one times seven", "147"),
            ("(12 + 3) × 4", "60"), ("two to the power of sixty", "1152921504606846976"),
            ("What is 0.1 plus 0.2?", "0.3"),
        ):
            with self.subTest(question=question):
                run = await run_python(arithmetic_source(question))
                self.assertEqual((run.status, run.output), ("ok", expected))
        run = await run_python("import statistics, sqlite3\n"
                               "db = sqlite3.connect('demo.db')\n"
                               "rows = db.execute('select 8 union all select 4')\n"
                               "print(statistics.median(row[0] for row in rows))")
        self.assertEqual((run.status, run.output), ("ok", "6.0"))

    async def test_private_host_files_environment_and_scratch_are_isolated(self):
        with tempfile.NamedTemporaryFile() as host, patch.dict(os.environ, NEX_TEST_SECRET="private"):
            source = ("import os\nfrom pathlib import Path\n"
                      f"assert not Path({host.name!r}).exists()\n"
                      "assert 'NEX_TEST_SECRET' not in os.environ\n"
                      "assert not any(Path(p).exists() for p in ('/srv/data', '/home', '/run', '/proc'))\n"
                      "assert os.getuid() != 0\n"
                      "Path('scratch').write_text('temporary')\nprint(Path('scratch').read_text())")
            run = await run_python(source)
        self.assertEqual((run.status, run.output), ("ok", "temporary"))
        run = await run_python("from pathlib import Path\nprint(Path('scratch').exists())")
        self.assertEqual((run.status, run.output), ("ok", "False"))

    async def test_network_process_creation_and_runtime_writes_are_denied(self):
        for source in ("import socket\nsocket.socket()", "import os\nos.fork()",
                       "open('/runner.py', 'a')", "open('/usr/bin/python3', 'wb')"):
            with self.subTest(source=source):
                run = await run_python(source)
                self.assertEqual(run.status, "error")
                self.assertRegex(run.output, r"PermissionError|Read-only file system|Text file busy")

    async def test_resource_limits_output_deadline_and_cancellation(self):
        run = await run_python("import resource\nprint(resource.getrlimit(resource.RLIMIT_AS)[1])")
        self.assertEqual((run.status, run.output), ("ok", str(256 * 1024**2)))
        run = await run_python("bytearray(300 * 1024**2)")
        self.assertEqual(run.status, "error")
        self.assertIn("MemoryError", run.output)
        run = await run_python("print('x' * 100000)")
        self.assertEqual((run.status, len(run.output)), ("output_limit", MAX_OUTPUT))
        with patch("nex.coding.sandbox.WALL_SECONDS", 0.5):
            self.assertEqual((await run_python("import time\ntime.sleep(60)")).status, "timeout")
        sleeping = asyncio.create_task(run_python("import time\ntime.sleep(60)"))
        await asyncio.sleep(0.15)
        sleeping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(sleeping, 2)
        self.assertEqual((await run_python("print(7)")).output, "7")
