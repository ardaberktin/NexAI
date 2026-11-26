"""Segment the continuous microphone timeline independently of WebSocket packet sizes."""

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.vad.vad_analyzer import VADState

from ..transcription import has_spoken_text
from . import INPUT_RATE
from .verify import unfinished


class TurnAudio:
    def __init__(self, listener, started, finished, progress, *, room_context=False,
                 on_words=None, matches_words=has_spoken_text):
        self.listener = listener
        self.started = started
        self.finished = finished
        self.progress = progress
        self.on_words = on_words
        self.matches_words = matches_words
        self.heard_words = False
        self.frame_bytes = listener.vad.num_frames_required() * 2
        self.pending = bytearray()
        self.preroll = deque(maxlen=round(0.7 * INPUT_RATE * 2 / self.frame_bytes))
        self.position = self.last_progress = 0
        self.turn_id = None
        self.raw = bytearray()
        self.silence = 0
        self.room_audio = bytearray() if room_context else None
        self.context = b""

    async def receive(self, packet):
        if len(packet) % 2 or not 0 < len(packet) <= 16_384:
            raise ValueError("Expected 16 kHz mono PCM frames")
        self.pending.extend(packet)
        while len(self.pending) >= self.frame_bytes:
            frame = bytes(self.pending[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            await self.process(frame)

    async def process(self, frame):
        count = len(frame) // 2
        self.position += count
        if self.room_audio is not None and self.turn_id is None:
            self.room_audio.extend(frame)
            limit = INPUT_RATE * 16 + self.preroll.maxlen * self.frame_bytes
            if len(self.room_audio) > limit:
                del self.room_audio[:-limit]
        state = await self.listener.vad.analyze_audio(frame)
        voiced = state in (VADState.STARTING, VADState.SPEAKING)
        self.listener.smart.append_audio(frame, voiced)
        if self.turn_id is None:
            self.preroll.append(frame)
            if state is VADState.SPEAKING:
                self.raw = bytearray(b"".join(self.preroll))
                self.preroll.clear()
                if self.room_audio is not None:
                    del self.room_audio[-len(self.raw):]
                    self.context = bytes(self.room_audio[-INPUT_RATE * 16:])
                self.turn_id = str(uuid4())
                self.start_time = datetime.now(UTC) - timedelta(seconds=len(self.raw) / (2 * INPUT_RATE))
                await self.started(self.turn_id, self.position - len(self.raw) // 2)
                self.heard_words = False
                self.listener.begin()
                await self.feed(self.raw)
            elif self.progress and self.position - self.last_progress >= INPUT_RATE:
                self.last_progress = self.position
                await self.progress(self.position - len(self.preroll) * self.frame_bytes // 2)
            return

        self.raw.extend(frame)
        partial = await self.feed(frame)
        previous = self.silence
        self.silence = 0 if voiced else previous + count
        # A short thinking pause or self-correction belongs to the same speaker turn.
        check_at = int(INPUT_RATE * 0.60)
        incomplete = unfinished(partial)
        if previous < check_at <= self.silence and not incomplete:
            end, _ = await self.listener.smart.analyze_end_of_turn()
            if end is EndOfTurnState.COMPLETE:
                await self.finish()
                return
        max_pause = 2.0 if incomplete else 1.35
        if self.silence >= INPUT_RATE * max_pause or len(self.raw) >= 2 * INPUT_RATE * 22:
            await self.finish()

    async def feed(self, frame):
        partial = await asyncio.to_thread(self.listener.feed, frame)
        if self.on_words and not self.heard_words and self.matches_words(partial):
            self.heard_words = True
            await self.on_words(self.turn_id)
        return partial

    async def flush(self):
        # An explicit endpoint must include the final, possibly short network packet.
        if self.pending and self.turn_id:
            self.raw.extend(self.pending)
            self.position += len(self.pending) // 2
            await self.feed(bytes(self.pending))
            self.pending.clear()
        await self.finish()

    async def finish(self):
        if self.turn_id is None:
            return
        identifier, self.turn_id = self.turn_id, None
        audio, self.raw = self.raw, bytearray()
        if self.room_audio is not None:
            self.room_audio.extend(audio)
            del self.room_audio[:-INPUT_RATE * 16]
        self.silence = 0
        tentative = await asyncio.to_thread(self.listener.finish)
        self.listener.smart.clear()
        await self.finished(identifier, self.start_time, self.position, audio, tentative)
