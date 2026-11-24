"""Run a bounded Python process with no host workspace or network access."""

import asyncio
import logging
import signal
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .guest import MIB, READY

MAX_SOURCE = 12_000
MAX_OUTPUT = 8_192
WALL_SECONDS = 8
SLOTS = threading.BoundedSemaphore(2)
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Run:
    status: str
    output: str = ""
    exit_code: int | None = None


def sandbox_command():
    # The model runner mounts Nex assets and a host work directory. User programs
    # get only the system interpreter, libraries, time zone data and a fresh tmpfs.
    return (
        "/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns",
        "--new-session", "--die-with-parent", "--cap-drop", "ALL",
        "--uid", "65534", "--gid", "65534", "--hostname", "nex-python",
        "--clearenv", "--setenv", "HOME", "/work", "--setenv", "LC_ALL", "C.UTF-8",
        "--ro-bind", "/usr/bin/python3", "/usr/bin/python3",
        "--ro-bind", "/usr/lib", "/usr/lib", "--symlink", "usr/lib", "/lib",
        "--ro-bind-try", "/usr/lib64", "/usr/lib64", "--symlink", "usr/lib64", "/lib64",
        "--ro-bind-try", "/usr/share/zoneinfo", "/usr/share/zoneinfo",
        "--ro-bind", str(Path(__file__).with_name("guest.py").resolve()), "/runner.py",
        "--dev-bind", "/dev/null", "/dev/null",
        "--size", str(16 * MIB), "--tmpfs", "/work", "--symlink", "work", "/tmp",
        "--remount-ro", "/", "--chdir", "/work",
        "--", "/usr/bin/python3", "-I", "-S", "-B", "-u", "/runner.py",
    )


async def capture(process, source):
    process.stdin.write(source)
    process.stdin.close()
    ready = await process.stdout.readline()
    if ready != (READY + "\n").encode():
        LOG.warning("Python sandbox startup failed: %.240s", ready.decode(errors="replace").strip())
        return Run("unavailable")
    output = bytearray()
    while len(output) <= MAX_OUTPUT:
        chunk = await process.stdout.read(min(4096, MAX_OUTPUT + 1 - len(output)))
        if not chunk:
            break
        output.extend(chunk)
    value = output[:MAX_OUTPUT].decode("utf-8", errors="replace").rstrip()
    if len(output) > MAX_OUTPUT:
        return Run("output_limit", value)
    code = await process.wait()
    if code in {-signal.SIGXCPU, 128 + signal.SIGXCPU}:
        return Run("timeout", value)
    return Run("error" if code else "ok", value, code)


async def run_python(source):
    data = source.encode("utf-8")
    if len(data) > MAX_SOURCE:
        return Run("source_limit")
    if not SLOTS.acquire(blocking=False):
        return Run("busy")
    try:
        process = await asyncio.create_subprocess_exec(
            *sandbox_command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={}, limit=MAX_OUTPUT,
        )
        try:
            async with asyncio.timeout(WALL_SECONDS):
                return await capture(process, data)
        except TimeoutError:
            return Run("timeout")
        finally:
            # Killing the Bubblewrap supervisor also tears down its PID namespace.
            # Drain the bounded pipe on cancellation or overflow so wait can finish.
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()
    except OSError as error:
        LOG.warning("Could not launch Python sandbox: %s", error)
        return Run("unavailable")
    finally:
        SLOTS.release()
