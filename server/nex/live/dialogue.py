"""Keep interactive generation separate from queued archive work."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

import httpx

from ..daybook import DayContext
from ..model_context import (
    DEFAULT_CONTEXT,
    INPUT_TOKENS_PATH,
    ContextBudget,
    context_size,
    input_tokens,
    log_completion,
    response_options,
)
from .public_reply import public_reply
from .weather_location import Coordinates


@dataclass
class Turn:
    id: str
    device: str
    text: str
    language: str
    zone: str
    started: datetime
    journal: DayContext | None = None
    archived: bool = True
    speaker: str = "unverified"
    coordinates: Coordinates | None = None
    location_requested: bool = False


def reply(text, language, sources=(), engine="local_model"):
    return {"text": text, "language": language, "sources": list(sources), "engine": engine}


QWEN_THINKING_TOKENS = 256


class Dialogue:
    def __init__(self, memory, local_socket, mac_socket, *, local_model_id=None):
        self.memory = memory
        self.local = self.client(local_socket)
        self.mac = self.client(mac_socket) if mac_socket else None
        self.local_model_id = local_model_id
        self.local_context = DEFAULT_CONTEXT
        self.background_slot = asyncio.Lock()
        self.priority_waiters = 0
        self.priority_done = asyncio.Event()
        self.priority_done.set()
        self.idle = asyncio.Event()
        self.idle.set()
        self.become_busy = asyncio.Event()
        self.voice_busy = False
        self.classifiers = 0

    @staticmethod
    def client(path):
        return httpx.AsyncClient(base_url="http://localhost", timeout=httpx.Timeout(120, connect=0.6),
                                 transport=httpx.AsyncHTTPTransport(uds=str(path)))

    async def close(self):
        await self.local.aclose()
        if self.mac:
            await self.mac.aclose()

    async def wait_ready(self):
        for _ in range(120):
            try:
                await self.health(self.local)
                try:
                    props = await self.local.get("/props", timeout=2)
                    if props.is_success:
                        self.local_context = context_size(props.json(), DEFAULT_CONTEXT)
                except (httpx.HTTPError, ValueError):
                    pass
                await self.complete(self.local, [{"role": "user", "content": "Say ready."}], 4, immediate=True)
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
        raise RuntimeError("The Excalibur voice model failed to warm up")

    @staticmethod
    async def health(client):
        result = await client.get("/health", timeout=0.75)
        result.raise_for_status()

    def enter(self):
        self.voice_busy = True
        self.update_priority()

    def leave(self):
        self.voice_busy = False
        self.update_priority()

    def update_priority(self):
        if self.voice_busy or self.classifiers:
            self.idle.clear()
            self.become_busy.set()
        else:
            self.become_busy.clear()
            self.idle.set()

    @staticmethod
    def request(messages, limit, *, stream=False, thinking=False, reasoning_effort=None):
        result = {"messages": messages, "max_tokens": limit, "temperature": 0.4, "stream": stream} | response_options(messages)
        if not thinking:
            result["chat_template_kwargs"] = {"enable_thinking": False}
        elif reasoning_effort in {"low", "medium", "high"}:
            result["reasoning_effort"] = reasoning_effort
        return result

    async def completion_options(self, client, messages, limit, *, temperature, thinking, reasoning_effort,
                                 stream=False, immediate=False):
        use_thinking = (client is self.mac) if thinking is None else thinking
        options = self.request(messages, limit, stream=stream, thinking=use_thinking, reasoning_effort=reasoning_effort)
        if temperature is not None:
            options["temperature"] = temperature
        if immediate or client is not self.local or not self.local_model_id or self.local_model_id() != "qwen-3.5-4b":
            return await self.budget_completion(client, options)
        # Qwen shares its completion limit between reasoning, delimiters and the visible answer.
        options["chat_template_kwargs"] = {"enable_thinking": True}
        options["max_tokens"] += QWEN_THINKING_TOKENS + 8
        options = await self.budget_completion(client, options)
        options["reasoning_budget_tokens"] = max(0, min(QWEN_THINKING_TOKENS, options["max_tokens"] - limit - 8))
        return options

    async def complete(self, client, messages, limit, *, temperature=None, thinking=None, reasoning_effort=None,
                       immediate=False):
        started = monotonic()
        options = await self.completion_options(client, messages, limit, temperature=temperature, thinking=thinking,
                                                reasoning_effort=reasoning_effort, immediate=immediate)
        response = await client.post("/v1/chat/completions", json=options)
        response.raise_for_status()
        payload = response.json()
        log_completion("mac" if client is self.mac else "live", started, payload)
        return payload["choices"][0]["message"]["content"].strip()

    async def stream_complete(self, client, messages, limit, on_delta, *, temperature=None, thinking=None,
                              reasoning_effort=None):
        started = monotonic()
        options = await self.completion_options(client, messages, limit, temperature=temperature, thinking=thinking,
                                                reasoning_effort=reasoning_effort, stream=True)
        parts = []
        async with client.stream("POST", "/v1/chat/completions", json=options,
                                 headers={"Accept": "text/event-stream"}) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    choices = json.loads(payload).get("choices", [])
                except ValueError:
                    continue
                text = choices[0].get("delta", {}).get("content") if choices else None
                if text:
                    parts.append(text)
                    await on_delta(text)
        log_completion("mac" if client is self.mac else "live", started, None)
        return "".join(parts).strip()

    async def embed(self, texts):
        if not texts:
            return []
        clients = [client for client in (self.mac, self.local) if client is not None]
        for client in clients:
            try:
                response = await client.post("/v1/embeddings", json={"input": list(texts)}, timeout=6)
                if not response.is_success:
                    continue
                data = response.json().get("data", [])
                vectors = [item.get("embedding") for item in data if isinstance(item, dict)]
                if len(vectors) == len(texts) and all(isinstance(vec, list) and vec for vec in vectors):
                    return vectors
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                continue
        return None

    async def budget_completion(self, client, options):
        capacity = self.local_context
        if client is self.mac:
            try:
                props = await client.get("/props", timeout=2)
                capacity = context_size(props.json()) if props.is_success else DEFAULT_CONTEXT
            except (httpx.HTTPError, ValueError):
                pass
        budget = ContextBudget(options, capacity)
        for candidate in budget.candidates():
            try:
                response = await client.post(INPUT_TOKENS_PATH, json=candidate, timeout=2)
                count = input_tokens(response.json()) if response.is_success else None
            except (httpx.HTTPError, ValueError):
                count = None
            if fitted := budget.fit(candidate, count):
                return fitted
        return budget.remaining()

    async def classify(self, messages):
        # A standby decision has a short deadline even while a Daybook job is generating.
        self.classifiers += 1
        self.update_priority()
        try:
            async with asyncio.timeout(3.5):
                return await self.complete(self.local, messages, 4, temperature=0, thinking=False, immediate=True)
        except (TimeoutError, httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
            return ""
        finally:
            self.classifiers -= 1
            self.update_priority()

    async def live_reply(self, turn, history, emit, *, locate=None, read_clock=None):
        return await public_reply(self, turn, history, emit, locate, read_clock=read_clock), False

    async def background_text(self, messages, limit, *, temperature=None, thinking=None, reasoning_effort=None):
        self.priority_waiters += 1
        self.priority_done.clear()
        try:
            async with self.background_slot:
                return await self.complete_background(
                    messages, limit, temperature=temperature, thinking=thinking, reasoning_effort=reasoning_effort,
                )
        finally:
            self.priority_waiters -= 1
            if self.priority_waiters == 0:
                self.priority_done.set()

    async def stream_background(self, messages, limit, on_delta, *, temperature=None, thinking=None,
                                reasoning_effort=None):
        self.priority_waiters += 1
        self.priority_done.clear()
        try:
            async with self.background_slot:
                if self.mac:
                    try:
                        await self.health(self.mac)
                        return await self.stream_complete(
                            self.mac, messages, limit, on_delta,
                            temperature=temperature, thinking=thinking, reasoning_effort=reasoning_effort,
                        )
                    except (httpx.HTTPError, KeyError, ValueError):
                        pass
                await self.idle.wait()
                return await self.stream_complete(
                    self.local, messages, limit, on_delta,
                    temperature=temperature, thinking=thinking, reasoning_effort=reasoning_effort,
                )
        except (httpx.HTTPError, KeyError, ValueError):
            return ""
        finally:
            self.priority_waiters -= 1
            if self.priority_waiters == 0:
                self.priority_done.set()

    async def low_priority_text(self, messages, limit, *, temperature=None):
        while True:
            await self.priority_done.wait()
            async with self.background_slot:
                if self.priority_waiters:
                    continue
                return await self.complete_background(messages, limit, temperature=temperature)

    async def complete_background(self, messages, limit, *, temperature=None, thinking=None, reasoning_effort=None):
        if self.mac:
            try:
                await self.health(self.mac)
                return await self.complete(
                    self.mac, messages, limit, temperature=temperature,
                    thinking=thinking, reasoning_effort=reasoning_effort,
                )
            except (httpx.HTTPError, KeyError, ValueError):
                pass
        while True:
            await self.idle.wait()
            request = asyncio.create_task(self.complete(
                self.local, messages, limit, temperature=temperature,
                thinking=thinking, reasoning_effort=reasoning_effort,
            ))
            interrupt = asyncio.create_task(self.become_busy.wait())
            try:
                await asyncio.wait((request, interrupt), return_when=asyncio.FIRST_COMPLETED)
                if request.done():
                    return request.result()
            except (httpx.HTTPError, KeyError, ValueError):
                return ""
            finally:
                request.cancel()
                interrupt.cancel()
                await asyncio.gather(request, interrupt, return_exceptions=True)
