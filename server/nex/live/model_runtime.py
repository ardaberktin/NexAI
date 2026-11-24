"""Supervise Excalibur's Gemma process."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic

import httpx

from ..model_context import DEFAULT_CONTEXT

LOG = logging.getLogger(__name__)
MODEL_ID = "gemma"
MODEL_FILE = "gemma-3-4b-it-qat-Q4_0.gguf"


class ModelRuntime:
    def __init__(self, assets, runtime, marker):
        self.assets = Path(assets)
        self.weights = self.assets / MODEL_FILE
        self.socket = Path(runtime) / "model.sock"
        self.marker = Path(marker)
        self.process = None
        self.device = None
        self.lock = asyncio.Lock()

    def status(self):
        ready = self.device is not None and self.process is not None and self.process.returncode is None
        return {"active": MODEL_ID if ready else None, "device": self.device if ready else None,
                "models": [{"id": MODEL_ID, "name": "Gemma 3 · 4B", "available": self.weights.is_file()}]}

    def mark_managed(self):
        contents = f"{MODEL_ID}\n".encode()
        try:
            if self.marker.read_bytes() == contents:
                return
        except FileNotFoundError:
            pass
        # Keep the archive on the managed route even while Gemma is unavailable.
        with NamedTemporaryFile(dir=self.marker.parent, delete=False) as file:
            path = Path(file.name)
            try:
                file.write(contents)
                file.flush()
                os.fsync(file.fileno())
                path.replace(self.marker)
            finally:
                path.unlink(missing_ok=True)

    async def stop(self):
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 6)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        self.process = self.device = None
        self.socket.unlink(missing_ok=True)

    async def ready(self):
        deadline = monotonic() + 90
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket))
        async with httpx.AsyncClient(transport=transport, timeout=1) as client:
            while self.process.returncode is None and monotonic() < deadline:
                try:
                    if (await client.get("http://localhost/health")).status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        return False

    async def start(self):
        if not self.weights.is_file():
            raise RuntimeError(f"Missing Excalibur model weights: {MODEL_FILE}")
        runtimes = [("CPU", self.assets, ["-ngl", "0"])]
        if os.environ.get("NEX_LIVE_DEVICE") != "cpu":
            runtimes.insert(0, ("Vulkan", self.assets / "vulkan", ["--device", "Vulkan0", "-ngl", "99"]))
        for device, directory, flags in runtimes:
            binary = directory / "llama-b12359" / "llama-server"
            if not binary.is_file():
                continue
            self.socket.unlink(missing_ok=True)
            try:
                self.process = await asyncio.create_subprocess_exec(
                    str(binary), "--model", str(self.weights), "--host", str(self.socket), "--reasoning", "off",
                    "--no-ui", "-t", "3", "-c", str(DEFAULT_CONTEXT), "--parallel", "1", "-lv", "1", *flags,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await self.ready():
                    self.device = device
                    LOG.info("Started Excalibur model %s on %s", MODEL_ID, device)
                    return
                LOG.warning("%s on %s did not become ready (exit status: %s)",
                            MODEL_ID, device, self.process.returncode)
            except OSError:
                LOG.warning("Could not launch %s on %s", MODEL_ID, device)
            await self.stop()
        raise RuntimeError("The Excalibur model could not start on GPU or CPU.")

    async def select(self, selected):
        if selected != MODEL_ID or not self.weights.is_file():
            raise ValueError("This model is unavailable on Excalibur.")
        return await self.ensure_started()

    async def ensure_started(self):
        async with self.lock:
            if self.status()["active"] is None:
                await self.stop()
                try:
                    await self.start()
                except (RuntimeError, OSError, asyncio.CancelledError):
                    await self.stop()
                    raise
            return self.status()

    async def monitor(self):
        while True:
            await asyncio.sleep(2)
            if not self.lock.locked() and self.status()["active"] is None:
                try:
                    await self.ensure_started()
                except (OSError, RuntimeError):
                    LOG.exception("Excalibur model restart failed")

    @contextlib.asynccontextmanager
    async def running(self):
        try:
            self.mark_managed()
            await self.ensure_started()
            watchdog = asyncio.create_task(self.monitor())
            try:
                yield self
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
        finally:
            await self.stop()
