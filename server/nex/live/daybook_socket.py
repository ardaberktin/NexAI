"""Private bridge from the authenticated archive API to the loaded voice model."""

import asyncio
import json
from contextlib import asynccontextmanager

from ..model_context import ModelMessages

MAX_REQUEST = 262_144


@asynccontextmanager
async def daybook_socket(path, dialogue):
    async def complete(reader, writer):
        try:
            async with asyncio.timeout(32):
                raw = await reader.readline()
                body = json.loads(raw)
                messages = ModelMessages(body["messages"], body.get("response_format"))
                if body.get("decision") is True:
                    async with asyncio.timeout(4.5):
                        await dialogue.idle.wait()
                        text = await dialogue.classify(messages)
                else:
                    sampling = {"temperature": 0} if body.get("temperature") == 0 else {}
                    text = await dialogue.background_text(messages, body["max_tokens"], **sampling)
                writer.write(json.dumps({"text": text}).encode() + b"\n")
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
