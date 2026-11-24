"""Own one llama.cpp process and persist successful Excalibur model selections."""

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
MODELS = {
    "qwen": ("Qwen 3 · 1.7B", "qwen.gguf"),
    "gemma": ("Gemma 3 · 4B", "gemma-3-4b-it-qat-Q4_0.gguf"),
}


class ModelRuntime:
    def __init__(self, assets, runtime, preference):
        self.assets = Path(assets)
        self.socket = Path(runtime) / "model.sock"
        self.preference = Path(preference)
        self.active = None
        self.process = None
        self.device = None
        self.lock = asyncio.Lock()

    def status(self):
        running = self.process is not None and self.process.returncode is None
        return {"active": self.active if running else None, "device": self.device if running else None,
                "models": [{"id": key, "name": name, "available": (self.assets / file).is_file()}
                           for key, (name, file) in MODELS.items()]}

    def saved(self):
        try:
            selected = self.preference.read_text().strip()
        except FileNotFoundError:
            return "qwen"
        return selected if selected in MODELS and (self.assets / MODELS[selected][1]).is_file() else "qwen"

    def persist(self, selected):
        with NamedTemporaryFile(mode="w", dir=self.preference.parent, delete=False) as file:
            path = Path(file.name)
            try:
                file.write(selected + "\n")
                file.flush()
                os.fsync(file.fileno())
                path.replace(self.preference)
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
        self.process = self.active = self.device = None
        self.socket.unlink(missing_ok=True)

    async def ready(self):
        deadline = monotonic() + 35
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

    async def start(self, selected):
        model = self.assets / MODELS[selected][1]
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
                    str(binary), "--model", str(model), "--host", str(self.socket), "--reasoning", "off",
                    "--no-ui", "-t", "3", "-c", str(DEFAULT_CONTEXT), "--parallel", "1", "-lv", "1", *flags,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await self.ready():
                    self.active, self.device = selected, device
                    LOG.info("Started Excalibur model %s on %s", selected, device)
                    return
            except OSError:
                LOG.warning("Could not launch %s on %s", selected, device)
            await self.stop()
        raise RuntimeError("The Excalibur model could not start on GPU or CPU.")

    async def select(self, selected):
        if selected not in MODELS or not (self.assets / MODELS[selected][1]).is_file():
            raise ValueError("This model is unavailable on Excalibur.")
        async with self.lock:
            if self.status()["active"] == selected:
                return self.status()
            previous = self.active
            await self.stop()
            try:
                await self.start(selected)
                self.persist(selected)
            except (RuntimeError, OSError, asyncio.CancelledError):
                await self.stop()
                if previous and previous != selected:
                    await self.start(previous)
                raise
            return self.status()

    async def monitor(self):
        while True:
            await asyncio.sleep(2)
            if not self.lock.locked() and self.status()["active"] is None:
                try:
                    await self.select(self.saved())
                except (OSError, RuntimeError, ValueError):
                    LOG.exception("Excalibur model restart failed")

    @contextlib.asynccontextmanager
    async def running(self):
        try:
            selected = self.saved()
            try:
                await self.select(selected)
            except RuntimeError:
                if selected == "qwen":
                    raise
                await self.select("qwen")
            watchdog = asyncio.create_task(self.monitor())
            try:
                yield self
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
        finally:
            await self.stop()
