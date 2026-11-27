"""Local live voice ASGI service with the same trust boundary as the Nex client API."""

import asyncio
import logging
import os
import socket
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from ..__main__ import read_access
from ..daybook import DayContext
from ..hosted.reset import restart_process, workspace_lock
from ..network import TAILSCALE_INTERFACE, TailnetEndpoint
from ..recall_dates import time_zone
from ..workspace_features import is_enabled, off_message
from .access import LiveAccess, authenticate
from .attention import MAX_AUDIO, MAX_CONTEXT, MIN_AUDIO
from .followup_gate import FollowupGate
from .model_api import install_model_api
from .model_runtime import ModelRuntime
from .room_context import RoomContext
from .session import Session
from .speech import Speech
from .standby import install_standby
from .verify import Verifier
from .voice_enrollment import install_voice_enrollment
from .watch_reply_voice import install_watch_reply_voice
from .workspaces import LiveWorkspaces

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
        speech = await asyncio.to_thread(Speech, ASSETS)
        room = await asyncio.to_thread(RoomContext.load, ASSETS)
        assets = Path(os.environ.get("NEX_WHISPER_ASSETS", str(DATA / "assets")))
        mac = None if TEST_LOOPBACK else RUNTIME.parent / "nex" / "mac-model.sock"
        workspaces = LiveWorkspaces(
            DATA, RUNTIME, read_access(DATA)["keys"]["admin"], speech, room, Verifier(assets, RUNTIME),
            models.socket if models else RUNTIME / "model.sock", mac,
            local_model_id=(lambda: models.selected_id) if models else None,
        )
        app.state.workspaces = workspaces
        await stack.enter_async_context(workspaces.running())
        maintenance = asyncio.create_task(watch_maintenance(app, workspaces.accounts))
        try:
            yield
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)


async def watch_maintenance(app, accounts):
    while True:
        await asyncio.sleep(0.5)
        try:
            if await asyncio.to_thread(accounts.resets_pending):
                app.state.restart_for_maintenance()
                return
        except sqlite3.Error as error:
            logging.getLogger(__name__).warning("Account maintenance check deferred (%s)", type(error).__name__)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(LiveAccess, workspaces=lambda: app.state.workspaces, loopback=TEST_LOOPBACK)


install_model_api(app, authenticate)
install_standby(app, authenticate)
install_voice_enrollment(app, lambda request: request.scope.get("nex.client"))
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
    state = request.scope["nex.workspace"]
    language = request.headers.get("x-nex-language", "auto")
    if language not in {"auto", "en", "tr"}:
        raise HTTPException(status_code=400)
    context_header = request.headers.get("x-nex-context-bytes", "0")
    if not context_header.isascii() or not context_header.isdecimal() or len(context_header) > 6:
        raise HTTPException(status_code=400)
    context_bytes = int(context_header)
    if context_bytes > MAX_CONTEXT or context_bytes % 2:
        raise HTTPException(status_code=400)
    if state.active.locked() or state.attention.busy.locked():
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
    ticket = await state.attention.check(
        device, language, bytes(audio[context_bytes:]), state.active, context=bytes(audio[:context_bytes]),
    )
    return {"ticket": ticket}


@app.delete("/v2/attention/{ticket}")
async def decline_attention(ticket: str, request: Request):
    if not (device := authenticate(request)):
        raise HTTPException(status_code=401)
    request.scope["nex.workspace"].attention.tickets.decline(device, ticket)
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
    state = ws.scope.get("nex.workspace")
    if not device or state.active.locked():
        await ws.close(code=1008)
        return
    async with state.active:
        await ws.accept()
        session = None
        close_reason = "interrupted"
        dialogue = state.dialogue
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
                initial_question = state.attention.tickets.claim(device, start["attention_ticket"])
                if not initial_question:
                    raise ValueError("Automatic voice approval expired")
            session = Session(ws, device, language, zone, start.get("wake") is True,
                              state.speech, dialogue, state.verifier, journal=journal,
                              local_chime=start.get("local_chime") is True,
                              opening_chimed=(initial_question is not None or start.get("wake") is True)
                                             and start.get("opening_chimed") is True,
                              archive_progress=start.get("archive_progress") is True,
                              gate=(FollowupGate(state.attention)
                                    if initial_question and start.get("selective_followups") is True else None))
            session.voices = state.voices
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
    with workspace_lock(DATA) as accounts:
        run_live()
        if accounts.resets_pending():
            restart_process()


def run_live():
    options = {"access_log": False, "ws_max_size": 32_768, "proxy_headers": False, "timeout_graceful_shutdown": 2}
    if TEST_LOOPBACK:
        options.update(host="127.0.0.1", port=int(os.environ.get("NEX_LIVE_PORT", "18767")))
    else:
        endpoint = TailnetEndpoint.current(DATA)
        options.update(ssl_certfile=str(endpoint.certificate), ssl_keyfile=str(endpoint.key))
    server = uvicorn.Server(uvicorn.Config(app, **options))
    app.state.restart_for_maintenance = lambda: setattr(server, "should_exit", True)
    if TEST_LOOPBACK:
        server.run()
        return
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, TAILSCALE_INTERFACE.encode() + b"\0")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((endpoint.address, PORT))
        server.run(sockets=[sock])


if __name__ == "__main__":
    main()
