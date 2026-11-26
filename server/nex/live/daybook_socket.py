"""Private bridge from the authenticated archive API to the loaded voice model."""

import asyncio
import json
from contextlib import asynccontextmanager

from ..model_context import ModelMessages

MAX_REQUEST = 4_194_304


@asynccontextmanager
async def daybook_socket(path, dialogue):
    async def complete(reader, writer):
        try:
            async with asyncio.timeout(115):
                raw = await reader.readline()
                body = json.loads(raw)
                if "embed" in body:
                    vectors = await dialogue.embed(body["embed"])
                    writer.write(json.dumps({"embeddings": vectors}).encode() + b"\n")
                    await writer.drain()
                    return
                messages = ModelMessages(body["messages"], body.get("response_format"))
                if body.get("decision") is True:
                    async with asyncio.timeout(4.5):
                        await dialogue.idle.wait()
                        text = await dialogue.classify(messages)
                    writer.write(json.dumps({"text": text}).encode() + b"\n")
                    await writer.drain()
                    return
                options = {}
                if body.get("temperature") is not None:
                    options["temperature"] = body["temperature"]
                if "thinking" in body:
                    options["thinking"] = bool(body["thinking"])
                if body.get("reasoning_effort") in {"low", "medium", "high"}:
                    options["reasoning_effort"] = body["reasoning_effort"]
                if body.get("stream") is True:
                    async def send_delta(chunk):
                        writer.write(json.dumps({"delta": chunk}, ensure_ascii=False).encode() + b"\n")
                        await writer.drain()

                    text = await dialogue.stream_background(messages, body["max_tokens"], send_delta, **options)
                    writer.write(json.dumps({"text": text, "done": True}, ensure_ascii=False).encode() + b"\n")
                    await writer.drain()
                else:
                    text = await dialogue.background_text(messages, body["max_tokens"], **options)
                    writer.write(json.dumps({"text": text}, ensure_ascii=False).encode() + b"\n")
                    await writer.drain()
        except (TimeoutError, OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    path.unlink(missing_ok=True)
    listener = await asyncio.start_unix_server(complete, path=path, limit=MAX_REQUEST)
    try:
        async with listener:
            yield
    finally:
        path.unlink(missing_ok=True)
