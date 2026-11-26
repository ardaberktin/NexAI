"""Supervise Excalibur's language model process with multi-model and KV-quant support."""

import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic

import httpx

from ..model_context import DEFAULT_CONTEXT

LOG = logging.getLogger(__name__)
MODEL_ID = "gemma"
MODEL_FILE = "gemma-3-4b-it-qat-Q4_0.gguf"
FALLBACK_CONTEXT = 4096
EXTRA_MODEL_PATTERNS = (
    ("muse-glimmer", "Muse Glimmer · 30B", "muse-glimmer*.gguf", True),
    ("gemma-4-12b", "Gemma 4 · 12B", "gemma-4-12[bB]*.gguf", True),
    ("gemma-4", "Gemma 4", "gemma-4*.gguf", True),
    ("gemma-3-12b", "Gemma 3 · 12B", "gemma-3-12[bB]*.gguf", False),
    ("gemma-3-27b", "Gemma 3 · 27B", "gemma-3-27[bB]*.gguf", False),
)
VALID_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    path: Path
    projector: Path | None = None
    context_size: int = DEFAULT_CONTEXT
    reasoning: bool = False


def _configured_int(name, default, minimum, maximum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _find_projector(assets, model_path):
    stem = model_path.stem.lower()
    for candidate in sorted(assets.glob("mmproj*.gguf")):
        if candidate.is_file():
            key = candidate.stem.lower().removeprefix("mmproj-").removeprefix("mmproj_")
            if key and any(token in stem for token in key.split("-")[:2] if len(token) >= 3):
                return candidate
    direct = model_path.with_name(f"mmproj-{model_path.name}")
    return direct if direct.is_file() else None


class ModelRuntime:
    def __init__(self, assets, runtime, marker):
        self.assets = Path(assets)
        self.weights = self.assets / MODEL_FILE
        self.socket = Path(runtime) / "model.sock"
        self.marker = Path(marker)
        self.process = None
        self.device = None
        self.selected_id = MODEL_ID
        self.active_context = None
        self.active_multimodal = False
        self.lock = asyncio.Lock()

    def catalog(self):
        default_ctx = _configured_int("NEX_LIVE_CONTEXT", DEFAULT_CONTEXT, 2048, 65536)
        specs = {
            MODEL_ID: ModelSpec(
                MODEL_ID, "Gemma 3 · 4B", self.weights,
                projector=_find_projector(self.assets, self.weights) if self.weights.is_file() else None,
                context_size=default_ctx, reasoning=False,
            )
        }
        # Discover optional upgraded local GGUFs (never legacy qwen.gguf).
        if self.assets.is_dir():
            for model_id, display, pattern, reasoning in EXTRA_MODEL_PATTERNS:
                if model_id in specs:
                    continue
                for path in sorted(self.assets.glob(pattern)):
                    if path.is_file() and not path.name.lower().startswith("mmproj"):
                        specs[model_id] = ModelSpec(
                            model_id, display, path,
                            projector=_find_projector(self.assets, path),
                            context_size=default_ctx, reasoning=reasoning,
                        )
                        break
        catalog_file = self.assets / "models.json"
        if catalog_file.is_file():
            try:
                entries = json.loads(catalog_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                entries = []
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    model_id = entry.get("id")
                    filename = entry.get("file")
                    if (not isinstance(model_id, str) or not VALID_MODEL_ID.fullmatch(model_id)
                            or model_id == "qwen" or not isinstance(filename, str)):
                        continue
                    path = (self.assets / filename).resolve()
                    if not path.is_relative_to(self.assets.resolve()) or not path.is_file():
                        continue
                    projector = None
                    if isinstance(proj_name := entry.get("mmproj"), str) and proj_name:
                        proj_path = (self.assets / proj_name).resolve()
                        if proj_path.is_relative_to(self.assets.resolve()) and proj_path.is_file():
                            projector = proj_path
                    ctx = entry.get("context_size")
                    ctx = ctx if type(ctx) is int and 2048 <= ctx <= 65536 else default_ctx
                    specs[model_id] = ModelSpec(
                        model_id, str(entry.get("name") or model_id), path,
                        projector=projector or _find_projector(self.assets, path),
                        context_size=ctx, reasoning=bool(entry.get("reasoning", False)),
                    )
        return specs

    def current_spec(self):
        specs = self.catalog()
        return specs.get(self.selected_id) or specs[MODEL_ID]

    def status(self):
        specs = self.catalog()
        ready = self.device is not None and self.process is not None and self.process.returncode is None
        return {
            "active": self.selected_id if ready else None,
            "device": self.device if ready else None,
            "context_size": self.active_context if ready else None,
            "multimodal": self.active_multimodal if ready else False,
            "models": [
                {
                    "id": spec.id, "name": spec.name, "available": spec.path.is_file(),
                    "multimodal": spec.projector is not None, "reasoning": spec.reasoning,
                }
                for spec in specs.values()
            ],
        }

    def mark_managed(self):
        # Preserve a valid persisted model selection; retire legacy markers like qwen back to gemma.
        specs = self.catalog()
        try:
            saved = self.marker.read_text(encoding="utf-8").strip()
            if saved in specs and specs[saved].path.is_file():
                self.selected_id = saved
        except FileNotFoundError:
            pass
        contents = f"{self.selected_id}\n".encode()
        try:
            if self.marker.read_bytes() == contents:
                return
        except FileNotFoundError:
            pass
        # Keep the archive on the managed route even while the selected model is unavailable.
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
        self.process = self.device = self.active_context = None
        self.active_multimodal = False
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

    def _launch_profiles(self, spec):
        parallel = str(_configured_int("NEX_LIVE_PARALLEL", 1, 1, 4))
        threads = str(_configured_int("NEX_LIVE_THREADS", 3, 1, 16))
        kv_flags = ["-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0"]
        mm_flags = ["--mmproj", str(spec.projector)] if spec.projector and spec.projector.is_file() else []
        reasoning = "auto" if spec.reasoning else "off"
        profiles = []
        if os.environ.get("NEX_LIVE_DEVICE") != "cpu":
            # Prefer Vulkan with KV-cache quantization and expanded context; fall back safely if VRAM is tight.
            profiles.append((
                "Vulkan", self.assets / "vulkan", spec.context_size,
                ["--device", "Vulkan0", "-ngl", "99", *kv_flags, *mm_flags],
                threads, parallel, reasoning,
            ))
            if spec.context_size > FALLBACK_CONTEXT:
                profiles.append((
                    "Vulkan", self.assets / "vulkan", FALLBACK_CONTEXT,
                    ["--device", "Vulkan0", "-ngl", "99"],
                    threads, "1", "off",
                ))
        profiles.append((
            "CPU", self.assets, spec.context_size,
            ["-ngl", "0", *kv_flags, *mm_flags],
            threads, parallel, reasoning,
        ))
        if spec.context_size > FALLBACK_CONTEXT:
            profiles.append((
                "CPU", self.assets, FALLBACK_CONTEXT,
                ["-ngl", "0"],
                threads, "1", "off",
            ))
        return profiles

    async def start(self):
        spec = self.current_spec()
        if not spec.path.is_file():
            raise RuntimeError(f"Missing Excalibur model weights: {spec.path.name}")
        for device, directory, ctx, flags, threads, parallel, reasoning in self._launch_profiles(spec):
            binary = directory / "llama-b12359" / "llama-server"
            if not binary.is_file():
                continue
            self.socket.unlink(missing_ok=True)
            try:
                self.process = await asyncio.create_subprocess_exec(
                    str(binary), "--model", str(spec.path), "--host", str(self.socket),
                    "--reasoning", reasoning, "--no-ui", "-t", threads, "-c", str(ctx),
                    "--parallel", parallel, "-lv", "1", *flags,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await self.ready():
                    self.device = device
                    self.active_context = ctx
                    self.active_multimodal = "--mmproj" in flags
                    LOG.info("Started Excalibur model %s on %s (ctx=%d mm=%s)",
                             spec.id, device, ctx, self.active_multimodal)
                    return
                LOG.warning("%s on %s (ctx=%d) did not become ready (exit status: %s)",
                            spec.id, device, ctx, self.process.returncode)
            except OSError:
                LOG.warning("Could not launch %s on %s", spec.id, device)
            await self.stop()
        raise RuntimeError("The Excalibur model could not start on GPU or CPU.")

    async def select(self, selected):
        specs = self.catalog()
        spec = specs.get(selected)
        if spec is None or not spec.path.is_file():
            raise ValueError("This model is unavailable on Excalibur.")
        async with self.lock:
            if self.selected_id != selected:
                await self.stop()
                self.selected_id = selected
                self.mark_managed()
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
