"""Listen on each recording device without occupying the interactive conversation slot."""

import asyncio
import re
from time import monotonic

from fastapi import WebSocket, WebSocketDisconnect

from ..wake import spoken_wake_body
from .turn_audio import TurnAudio

# Only an explicit name can commit pickup from a streaming (still revisable) transcript.
WAKE_PREFIX = re.compile(
    r"^(?:(?:hey|selam|merhaba)[\s,]+(?:nex|neks|next|necks)\b|hey[\s,]+nax\b|(?:nex|neks)[\s,.:!?-])",
    re.IGNORECASE,
)
from .access import workspace_for


class Standby:
    def __init__(self, state, ws, device, language, *, wake_chime=False):
        self.state, self.ws = state, ws
        self.device, self.language = device, language
        self.listener = state.speech.listen(language)
        self.wake_chime = wake_chime
        self.wake_token = None
        self.tickets = state.attention.tickets
        self.input = TurnAudio(self.listener, self.started, self.finished, None, room_context=True,
                               on_words=self.heard_name if wake_chime else None, matches_words=WAKE_PREFIX.match)
        self.accepted = False

    async def started(self, _identifier, start):
        pass

    async def heard_name(self, _identifier):
        if self.tickets.waking(self.device, self.wake_token):
            return
        self.wake_token = self.tickets.reserve_wake(self.device, self.state.active)
        if self.wake_token:
            await self.ws.send_json({"kind": "wake_chime", "wake_token": self.wake_token})

    def release_wake(self):
        if self.tickets.waking(self.device, self.wake_token):
            self.tickets.decline(self.device, self.wake_token)
        self.wake_token = None

    async def finished(self, _identifier, _started, _end, audio, text):
        if self.wake_chime and spoken_wake_body(text) is not None:
            await self.heard_name(_identifier)
        ticket = await self.state.attention.check(
            self.device, self.language, bytes(audio), self.state.active,
            context=self.input.context, streaming_text=text, wake_token=self.wake_token,
        )
        if self.wake_chime and not ticket:
            await self.keep_wake_during_pause(_identifier)
        named = ticket is not None and self.tickets.pending.named
        try:
            await self.ws.send_json({"kind": "decision", "ticket": ticket, "named": named})
            self.accepted = ticket is not None
        finally:
            if ticket and not self.accepted:
                self.tickets.decline(self.device, ticket)

    async def keep_wake_during_pause(self, identifier):
        until, explicit = self.state.attention.pending_wake.get(self.device, (0, False))
        remaining = until - monotonic()
        if explicit and remaining > 0:
            await self.heard_name(identifier)
            if self.tickets.waking(self.device, self.wake_token):
                self.tickets.expire_after(remaining)
        else:
            self.release_wake()

    async def run(self):
        try:
            await self.listener.prime_vad()
            await self.ws.send_json({"kind": "ready", "privacy_version": 1})
            while not self.accepted:
                packet = await asyncio.wait_for(self.ws.receive_bytes(), 20)
                if not 0 < len(packet) <= 16_384 or len(packet) % 2:
                    raise ValueError("Invalid microphone frame")
                await self.input.receive(packet)
        finally:
            self.release_wake()
            await self.listener.smart.cleanup()
            await self.listener.vad.cleanup()


def install_standby(app, authenticate):
    devices = set()

    @app.websocket("/v2/listen")
    @app.websocket("/v1/listen")
    async def listen(ws: WebSocket):
        device = authenticate(ws)
        language = ws.query_params.get("language", "auto")
        state = workspace_for(ws)
        slot = (id(state), device)
        if not device or slot in devices or language not in {"auto", "en", "tr"}:
            await ws.close(code=1008)
            return
        devices.add(slot)
        try:
            await ws.accept()
            await Standby(state, ws, device, language, wake_chime=ws.query_params.get("wake_chime") == "1").run()
            await ws.close(code=1000)
        except WebSocketDisconnect:
            pass
        except (KeyError, TypeError, ValueError, TimeoutError):
            await ws.close(code=1008)
        finally:
            devices.discard(device)
