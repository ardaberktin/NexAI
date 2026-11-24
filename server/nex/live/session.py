"""A bounded duplex conversation and its relationship to archived turns."""

import asyncio
import re
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

import httpx

from ..conversation_history import MAX_EXCHANGES, time_key
from ..conversation_intent import check_earlier_audio, earlier_question
from ..conversation_recall import last_recall
from ..voice_chime import CHIME_PCM
from ..wake import next_wake_body, spoken_wake_body
from . import OUTPUT_RATE
from .attention import confirm_wake_transcript, possible_request
from .control import control_payload
from .device_clock import DeviceClockTurn
from .dialogue import Turn, reply
from .turn_audio import TurnAudio
from .weather_location import WeatherLocationTurn

NEXT = object()


class Session:
    def __init__(self, ws, device, language, zone, wake, speech, dialogue, verifier,
                 *, journal=None, archive_progress=False, gate=None, local_chime=False, opening_chimed=False):
        self.ws, self.device, self.language, self.zone = ws, device, language, zone
        self.wake, self.speech, self.dialogue = wake, speech, dialogue
        self.listening = speech.listen(language)
        self.verifier = verifier
        self.voices = None
        self.weather_location_supported = False
        self.weather_location = None
        self.clock_supported = False
        self.device_clock = None
        self.journal = journal
        self.gate = gate
        self.local_chime = local_chime
        self.opening_chimed = opening_chimed
        self.chimed_turn = None
        self.candidate_start = 0
        self.input = TurnAudio(self.listening, self.start_turn, self.finish_turn,
                               self.progress if archive_progress else None, room_context=gate is not None,
                               on_words=self.chime if local_chime and gate is None else None)
        self.playback = None
        self.history = []
        self.sending = asyncio.Lock()
        self.closed = False
        self.last_turn = monotonic()
        self.audible_until = self.last_turn

    def timed_out(self):
        responding = self.playback and not self.playback.done()
        return (not self.input.turn_id and not responding
                and monotonic() - max(self.last_turn, self.audible_until) > 45)

    async def event(self, kind, **data):
        async with self.sending:
            await self.ws.send_json({"kind": kind, **data})

    async def progress(self, end):
        await self.event("progress", end=end)

    async def start_turn(self, identifier, start):
        if self.gate:
            self.candidate_start = start
            return
        await self.accept_turn(identifier, start)

    async def accept_turn(self, identifier, start):
        self.last_turn = monotonic()
        await self.interrupt()
        self.dialogue.enter()
        await self.event("turn", id=identifier, start=start)

    async def finish_turn(self, identifier, started, end, audio, tentative):
        text = await self.verifier.check(audio, self.language, tentative)
        if self.gate:
            text = await self.gate.resolve(text, audio, self.input.context, self.language, history=self.history[-4:])
            if not text:
                await self.progress(end)
                return
            await self.accept_turn(identifier, self.candidate_start)
        if self.wake:
            text = await confirm_wake_transcript(self.verifier, audio, self.language, text)
        transcript = text
        while (body := spoken_wake_body(text)) is not None:
            text = body
        alternate = next_wake_body(text)
        if self.wake and (alternate == "" or (alternate and possible_request(alternate, allow_question_mark_only=False))):
            text = alternate
        if text:
            await self.chime(identifier)
            self.opening_chimed = False
            if check_earlier_audio(text) and last_recall(self.history):
                alternative = await self.verifier.recover(audio, self.language)
                if earlier_question(alternative):
                    text = transcript = alternative
        await asyncio.to_thread(self.dialogue.memory.save_result, identifier, self.device, transcript=transcript)
        await self.event("turn_end", id=identifier, end=end, transcript=text)
        if not text:
            self.dialogue.leave()
            if self.wake:
                await self.event("not_addressed")
            return
        self.wake = False
        turn = Turn(identifier, self.device, text, self.language, self.zone, started, self.journal)
        self.start_reply(turn, audio)

    async def begin_with_question(self, text, *, audio=None):
        # The opening is already in the ambient journal. Its followups wait for acceptance.
        self.input.on_words = None
        identifier = str(uuid4())
        self.last_turn = monotonic()
        self.dialogue.enter()
        await self.event("turn", id=identifier)
        await self.chime(identifier)
        self.opening_chimed = False
        await self.event("turn_end", id=identifier, transcript=text)
        turn = Turn(identifier, self.device, text, self.language, self.zone, datetime.now(UTC), archived=False)
        self.start_reply(turn, audio)

    async def chime(self, identifier):
        if self.opening_chimed or self.chimed_turn == identifier:
            return
        self.chimed_turn = identifier
        if self.local_chime:
            await self.event("chime", id=identifier)
        else:
            await self.send_audio(CHIME_PCM)

    def start_reply(self, turn, audio):
        self.playback = asyncio.create_task(self.respond(turn, audio))

    def control_result(self, raw):
        message = control_payload(raw)
        kind = message.get("kind")
        if kind == "weather_location_result" and self.weather_location_supported:
            pending = self.weather_location
        elif kind == "clock_result" and self.clock_supported:
            pending = self.device_clock
        else:
            raise ValueError("Unexpected voice control message")
        if pending:
            pending.receive(message)

    async def read_clock(self, identifier):
        pending = DeviceClockTurn(identifier)
        self.device_clock = pending
        try:
            await self.event("clock_request", id=identifier)
            return await pending.wait()
        finally:
            if self.device_clock is pending:
                self.device_clock = None

    async def locate_weather(self, turn):
        pending = WeatherLocationTurn(turn.id)
        self.weather_location = pending
        try:
            await self.event("weather_location_request", id=turn.id)
            turn.location_requested = True
            turn.coordinates = await pending.wait()
        finally:
            if self.weather_location is pending:
                self.weather_location = None

    async def interrupt(self):
        self.audible_until = monotonic()
        if not self.playback:
            return
        task, self.playback = self.playback, None
        if not task.done():
            task.cancel()
        # Flush audio already queued on the client even if generation finished.
        await self.event("stop_audio")
        await asyncio.gather(task, return_exceptions=True)

    async def respond(self, turn, audio):
        phrases = asyncio.Queue()
        speaking = asyncio.create_task(self.speak(phrases))

        def enqueue(text, language):
            phrases.put_nowait((text, language))

        try:
            if self.voices and audio:
                turn.speaker = await asyncio.to_thread(self.voices.identify, bytes(audio))
            await self.event("speaker", id=turn.id, speaker=turn.speaker)
            history = self.history.copy()
            locate = self.locate_weather if self.weather_location_supported else None
            clock = self.read_clock if self.clock_supported else None
            result, _ = await self.dialogue.live_reply(turn, history, enqueue, locate=locate, read_clock=clock)
            if result["engine"] == "private_request":
                self.history.clear()
            elif result["text"]:
                await asyncio.to_thread(self.dialogue.memory.save_turn_reply, turn, result)
                self.history.append({"kind": "question", "id": "q-" + turn.id, "text": turn.text,
                                     "started": time_key(turn.started)})
                self.history.append({"kind": "answer", "id": "a-" + turn.id, **result})
                self.history = self.history[-2 * MAX_EXCHANGES:]
            phrases.put_nowait(NEXT)
            await speaking
            await self.event("reply", id=turn.id, reply=result)
        except (httpx.HTTPError, KeyError, ValueError):
            result = reply("", turn.language, engine="model_unavailable")
            if not speaking.done():
                phrases.put_nowait(NEXT)
                await speaking
            await self.event("reply", id=turn.id, reply=result)
        finally:
            speaking.cancel()
            await asyncio.gather(speaking, return_exceptions=True)
            self.last_turn = monotonic()
            if self.input.turn_id is None:
                self.dialogue.leave()

    async def speak(self, phrases):
        while (phrase := await phrases.get()) is not NEXT:
            text, language = phrase
            # Speak plain text while retaining the formatted reply in the archive.
            text = re.sub(r"[*#`]", "", text).strip()
            if not text:
                continue
            iterator = self.speech.speak(text, language)
            while chunk := await asyncio.to_thread(next, iterator, None):
                await self.send_audio(chunk)

    async def send_audio(self, chunk):
        async with self.sending:
            await self.ws.send_bytes(chunk)
            self.audible_until = max(monotonic(), self.audible_until) + len(chunk) / (2 * OUTPUT_RATE)

    async def close(self):
        self.closed = True
        if self.playback and not self.playback.done():
            self.playback.cancel()
            await asyncio.gather(self.playback, return_exceptions=True)
        await self.listening.smart.cleanup()
        await self.listening.vad.cleanup()
