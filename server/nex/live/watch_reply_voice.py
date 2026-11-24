"""Render a finished, recorder-safe watch reply with the live conversation voice."""

import asyncio
import io
import logging
from time import monotonic
from uuid import UUID

import numpy as np
import soundfile as sf
from fastapi import HTTPException, Request, Response

from ..http_api import safe_voice_reply
from ..privacy.voice import voice_reply_text
from ..voice_stream import SpeechChunks
from . import OUTPUT_RATE

MAX_SECONDS = 120
MAX_TEXT = 1800


def render_reply(speech, text, language):
    output = io.BytesIO()
    frames = 0
    with sf.SoundFile(output, mode="w", samplerate=OUTPUT_RATE, channels=1, format="MP3") as clip:
        def write(sentence, language):
            nonlocal frames
            for pcm in speech.speak(sentence.strip(), language):
                samples = np.frombuffer(pcm, dtype="<i2")
                frames += len(samples)
                if frames > OUTPUT_RATE * MAX_SECONDS:
                    raise ValueError("Watch reply exceeds the audio limit.")
                clip.write(samples)

        chunks = SpeechChunks(write)
        chunks.add(text, language)
        chunks.flush(language, final=True)
        if not frames:
            raise ValueError("Voice produced no sound.")
    return output.getvalue()


def install_watch_reply_voice(app, authenticate):
    slot = asyncio.Lock()

    @app.get("/v2/watch/recordings/{identifier}/voice")
    async def watch_reply_voice(identifier: UUID, request: Request):
        if authenticate(request) != "iphone":
            raise HTTPException(403)
        row = await asyncio.to_thread(app.state.dialogue.memory.recording, str(identifier))
        if not row or row["device"] != "watch" or row["mode"] != "voice" or row["state"] != "ready":
            raise HTTPException(404)
        reply = row["reply"]
        if not safe_voice_reply(reply) or not (text := voice_reply_text(reply)):
            raise HTTPException(404)
        if len(text) > MAX_TEXT or reply.get("language") not in {"en", "tr"}:
            raise HTTPException(422)
        if slot.locked():
            raise HTTPException(429)
        async with slot:
            started = monotonic()
            try:
                audio = await asyncio.to_thread(render_reply, app.state.speech, text, reply["language"])
            except (RuntimeError, ValueError, OSError) as exc:
                raise HTTPException(503, "Nex voice is unavailable.") from exc
        logging.getLogger("uvicorn.error").info("Watch reply voice rendered: bytes=%d seconds=%.2f",
                                               len(audio), monotonic() - started)
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
