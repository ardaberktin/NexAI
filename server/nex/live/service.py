"""Local live voice ASGI service with the same trust boundary as the Nex client API."""

import asyncio
import hmac
import ipaddress
import logging
import os
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from ..__main__ import read_access
from ..activities.worker import ActivityWorker
from ..daybook import DayContext
from ..daybook_briefing import Briefings
from ..live_memory import LiveMemory
from ..network import TAILNET, TAILSCALE_INTERFACE, TailnetEndpoint
from ..personal_files import PersonalFiles
from ..personal_learning import PersonalLearning
from ..privacy.recorded import speaker_socket
from ..recall_dates import time_zone
from ..workspace_features import is_enabled, off_message
from .attention import MAX_AUDIO, MAX_CONTEXT, MIN_AUDIO, Attention
from .daybook_socket import daybook_socket
from .dialogue import Dialogue
from .followup_gate import FollowupGate
from .model_api import install_model_api
from .model_runtime import ModelRuntime
from .room_context import RoomContext
from .session import Session
from .speech import Speech
from .standby import install_standby
from .verify import Verifier
from .voice_enrollment import install_voice_enrollment
from .voice_profiles import VoiceProfiles
from .watch_reply_voice import install_watch_reply_voice

PORT = 8767
DATA = Path(os.environ.get("NEX_DATA", "/srv/data/shared/nex-private"))
RUNTIME = Path(os.environ["NEX_LIVE_RUNTIME"])
ASSETS = DATA / "assets" / "live"
TEST_LOOPBACK = os.environ.get("NEX_LIVE_TEST_LOOPBACK") == "1"


@asynccontextmanager
async def lifespan(app):
    async with AsyncExitStack() as stack:
        models = None
        if os.environ.get("NEX_LIVE_MANAGED_MODEL") == "1":
            models = ModelRuntime(ASSETS, RUNTIME, DATA / "live-model")
            await stack.enter_async_context(models.running())
        app.state.models = models
        await stack.enter_async_context(voice_services(app, models))
        yield


@asynccontextmanager
async def voice_services(app, models):
    app.state.keys = read_access(DATA)["keys"]
    memory = LiveMemory(DATA)
    with memory.connect() as db:
        db.execute("SELECT 1 FROM live_turn_results LIMIT 1")
    app.state.speech = await asyncio.to_thread(Speech, ASSETS)
    mac = None if TEST_LOOPBACK or models else RUNTIME.parent / "nex" / "mac-model.sock"
    dialogue = Dialogue(memory, models.socket if models else RUNTIME / "qwen.sock", mac)
    await dialogue.wait_ready()
    app.state.dialogue = dialogue
    app.state.active = asyncio.Lock()
    model_assets = Path(os.environ.get("NEX_WHISPER_ASSETS", str(DATA / "assets")))
    app.state.verifier = Verifier(model_assets, RUNTIME)
    app.state.room = await asyncio.to_thread(RoomContext.load, ASSETS)
    app.state.voices = VoiceProfiles(memory.privacy, app.state.room, app.state.keys["admin"])
    app.state.attention = Attention(app.state.speech, dialogue, app.state.verifier, room=app.state.room)
    async with (daybook_socket(RUNTIME / "daybook.sock", dialogue),
                speaker_socket(RUNTIME / "speaker.sock", app.state.voices)):
        briefings = asyncio.create_task(Briefings(memory, dialogue.background_text).run())
        activities = asyncio.create_task(ActivityWorker(memory, RUNTIME, app.state.room, dialogue).run())
        learning = asyncio.create_task(PersonalLearning(memory, PersonalFiles(memory.root), dialogue.background_text).run())
        try:
            yield
        finally:
            briefings.cancel()
            learning.cancel()
            activities.cancel()
            await asyncio.gather(briefings, learning, activities, return_exceptions=True)
            await dialogue.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def authenticate(connection):
    client_ip = ipaddress.ip_address(connection.client.host) if connection.client else None
    if client_ip is None or (not TEST_LOOPBACK and client_ip not in TAILNET):
        return None
    token = connection.headers.get("authorization", "").removeprefix("Bearer ").encode()
    return next((device for device in ("mac", "iphone")
                 if hmac.compare_digest(token, app.state.keys[device].encode())), None)


install_model_api(app, authenticate)
install_standby(app, authenticate)
install_voice_enrollment(app, authenticate)
install_watch_reply_voice(app, authenticate)


@app.get("/v2/privacy/version")
@app.get("/v1/privacy/version")
async def privacy_version(request: Request):
    if not authenticate(request):
        raise HTTPException(401)
    return {"version": 1}


@app.post("/v2/attention")
@app.post("/v1/attention")
async def attention(request: Request):
    if not (device := authenticate(request)):
        raise HTTPException(status_code=401)
    language = request.headers.get("x-nex-language", "auto")
    if language not in {"auto", "en", "tr"}:
        raise HTTPException(status_code=400)
    context_header = request.headers.get("x-nex-context-bytes", "0")
    if not context_header.isascii() or not context_header.isdecimal() or len(context_header) > 6:
        raise HTTPException(status_code=400)
    context_bytes = int(context_header)
    if context_bytes > MAX_CONTEXT or context_bytes % 2:
        raise HTTPException(status_code=400)
    if app.state.active.locked() or app.state.attention.busy.locked():
        return {"ticket": None}
    audio = bytearray()
    try:
        async with asyncio.timeout(3):
            async for packet in request.stream():
                if len(audio) + len(packet) > MAX_AUDIO + context_bytes:
                    raise HTTPException(status_code=413)
                audio.extend(packet)
    except TimeoutError:
        raise HTTPException(status_code=408) from None
    if len(audio) - context_bytes < MIN_AUDIO or len(audio) % 2:
        return {"ticket": None}
    ticket = await app.state.attention.check(
        device, language, bytes(audio[context_bytes:]), app.state.active, context=bytes(audio[:context_bytes]),
    )
    return {"ticket": ticket}


@app.delete("/v2/attention/{ticket}")
async def decline_attention(ticket: str, request: Request):
    if not (device := authenticate(request)):
        raise HTTPException(status_code=401)
    app.state.attention.tickets.decline(device, ticket)
    return {"ok": True}


async def relay_audio(ws, session):
    while True:
        try:
            message = await asyncio.wait_for(ws.receive(), 20)
        except TimeoutError:
            await session.event("audio_timeout")
            await ws.close(code=1011, reason="microphone_timeout")
            return "microphone_timeout"
        if message["type"] == "websocket.disconnect":
            return f"peer_{message.get('code', 1006)}"
        if (audio := message.get("bytes")) is not None:
            await session.input.receive(audio)
            if session.timed_out():
                await session.event("idle")
                await ws.close(code=1000, reason="idle")
                return "idle"
        elif message.get("text") == "finish":
            await session.input.flush()
        elif message.get("text") == "interrupt":
            await session.interrupt()
        else:
            session.control_result(message.get("text"))


@app.websocket("/v2/live")
@app.websocket("/v1/live")
async def live(ws: WebSocket):
    await serve_live(ws, authenticate(ws))


@app.websocket("/v2/watch/live")
async def watch_live(ws: WebSocket):
    # The paired iPhone authenticates the relay. The Watch owns the audio and its archive entries.
    await serve_live(ws, "watch" if authenticate(ws) == "iphone" else None)


async def serve_live(ws: WebSocket, device):
    if not device or app.state.active.locked():
        await ws.close(code=1008)
        return
    async with app.state.active:
        await ws.accept()
        session = None
        close_reason = "interrupted"
        dialogue = app.state.dialogue
        try:
            start = await asyncio.wait_for(ws.receive_json(), 5)
            language = start.get("language")
            if language not in {"auto", "en", "tr"}:
                raise ValueError("Unsupported language")
            zone = time_zone(start.get("time_zone", "UTC")).key
            day = start.get("daybook_day")
            if day is not None and not await asyncio.to_thread(is_enabled, dialogue.memory, "daybook"):
                close_reason = "daybook_disabled"
                await ws.close(code=1008, reason=off_message("daybook"))
                return
            journal = DayContext.parse(day, zone, start.get("daybook_source"), start.get("daybook_focus")) if day is not None else None
            initial_question = None
            if "attention_ticket" in start:
                initial_question = app.state.attention.tickets.claim(device, start["attention_ticket"])
                if not initial_question:
                    raise ValueError("Automatic voice approval expired")
            session = Session(ws, device, language, zone, start.get("wake") is True,
                              app.state.speech, dialogue, app.state.verifier, journal=journal,
                              local_chime=start.get("local_chime") is True,
                              opening_chimed=(initial_question is not None or start.get("wake") is True)
                                             and start.get("opening_chimed") is True,
                              archive_progress=start.get("archive_progress") is True,
                              gate=(FollowupGate(app.state.attention)
                                    if initial_question and start.get("selective_followups") is True else None))
            session.voices = app.state.voices
            session.weather_location_supported = start.get("weather_location_supported") is True
            session.clock_supported = start.get("clock_supported") is True
            await session.listening.prime_vad()
            await session.event("ready", archive_progress=True, privacy_version=1)
            async with asyncio.timeout(900):
                if initial_question:
                    await session.begin_with_question(initial_question.text, audio=initial_question.audio)
                    initial_question = None
                close_reason = await relay_audio(ws, session)
        except WebSocketDisconnect as error:
            close_reason = f"peer_{error.code}"
        except TimeoutError:
            close_reason = "session_limit" if session else "handshake_timeout"
            if session:
                await session.event("idle")
            await ws.close(code=1000 if session else 1008, reason=close_reason)
        except (ValueError, TypeError, AttributeError):
            close_reason = "protocol_error"
            await ws.close(code=1008, reason=close_reason)
        finally:
            logging.getLogger("uvicorn.error").info(
                "Live voice ended: device=%s reason=%s input_samples=%d",
                device, close_reason, session.input.position if session else 0,
            )
            try:
                if session:
                    await session.close()
            finally:
                dialogue.leave()


def main():
    if TEST_LOOPBACK:
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("NEX_LIVE_PORT", "18767")),
                    access_log=False, ws_max_size=32_768, timeout_graceful_shutdown=2)
        return
    endpoint = TailnetEndpoint.current(DATA)
    config = uvicorn.Config(app, ssl_certfile=str(endpoint.certificate), ssl_keyfile=str(endpoint.key),
                            ws_max_size=32_768, access_log=False, proxy_headers=False, timeout_graceful_shutdown=2)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, TAILSCALE_INTERFACE.encode() + b"\0")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((endpoint.address, PORT))
        uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
