"""Analyze all uploaded clips independently of Whisper and interactive voice turns."""

import asyncio
import json
import logging
import sqlite3
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from ..audio_files import AudioFiles
from ..live import VOICE_LABELS
from ..models import Engines, ModelError
from ..recall_dates import time_key
from ..settings import Settings
from .jobs import ActivityJobs
from .recognition import (
    Observation,
    classifier_messages,
    device_playback_messages,
    from_sounds,
    interpret,
    interpret_device_playback,
)
from .screen_recognition import classify_with_screen
from .voice_origin import review_voice_origin, source_evidence

LOG = logging.getLogger(__name__)


def retry_time():
    return time_key(datetime.now(UTC) + timedelta(minutes=5))


class ActivityWorker:
    def __init__(self, store, runtime, room, dialogue, *, settings_root=None, voices=None):
        self.jobs = ActivityJobs(store)
        self.audio = AudioFiles(store.root / "audio")
        self.settings_root, self.runtime = settings_root or store.root, runtime
        self.room, self.dialogue, self.voices = room, dialogue, voices

    def listen(self, job):
        # Read the archive service's current decoder configuration, including changes made since launch.
        settings = Settings(self.settings_root)
        decoder = Engines(settings, self.runtime)
        with TemporaryDirectory(dir=self.runtime) as directory:
            path = decoder.decode(self.audio.path(job["recording_id"]), Path(directory), settings.read())
            with wave.open(str(path), "rb") as stream:
                pcm = stream.readframes(stream.getnframes())
        if hasattr(self.room, "clip_audio"):
            sounds, profile, samples, segments = self.room.clip_audio(pcm)
            if profile is not None and self.voices is not None and segments:
                owner = self.voices.owner_present(samples, segments)
                if owner is not None:
                    profile["owner_speaking"] = owner
            return sounds, profile
        return self.room.sound_labels(pcm), None

    async def analyze(self, job, audio):
        if audio:
            try:
                sounds, audio_context = await asyncio.to_thread(self.listen, job)
            except (ModelError, OSError, wave.Error, RuntimeError, ValueError) as error:
                await asyncio.to_thread(self.jobs.defer_audio, job, retry_time())
                LOG.warning("Activity audio deferred (%s)", type(error).__name__)
            else:
                await asyncio.to_thread(self.jobs.save_sounds, job, sounds, audio_context=audio_context)
            return
        sounds = json.loads(job["sounds"]) if job["sounds"] is not None else None
        audio_context = json.loads(job["audio_context"]) if job["audio_context"] else None
        device_context = json.loads(job["device_context"]) if job["device_context"] else None
        transcript, screen = (job["transcript"] or "")[:1500], job["screen"] or ""
        fallback = from_sounds(sounds, job["mode"], transcript)
        result = await self.classify(transcript, screen, sounds, fallback, device_context=device_context)
        result = await self.review_voices(job, result, transcript, sounds, audio_context, device_context, fallback)
        if (result is not None and result.category == "unknown"
                and device_context and device_context.get("media_playing")):
            device_result = await self.classify_device_playback(device_context, fallback)
            if device_result is not None:
                result = device_result
        await asyncio.to_thread(self.jobs.save, job, result or fallback, due=None if result else retry_time())

    async def review_voices(self, job, result, transcript, sounds, audio_context, device_context, fallback):
        if (result is None or result.basis == "screen" or result.category not in ("unknown", "speech", "conversation")
                or not (transcript or sounds)):
            return result
        if result.category == "unknown" and not (transcript or VOICE_LABELS.intersection(sounds or ())):
            return result
        nearby = await asyncio.to_thread(self.jobs.nearby_sounds, job)
        evidence = source_evidence(transcript, sounds, nearby,
                                   audio_context=audio_context, device_context=device_context)
        if (result.category == "unknown"
                and not (transcript or evidence["current_playback"] or evidence["earlier_playback"]
                         or evidence.get("device_playback"))):
            return result
        reviewed = await review_voice_origin(self.dialogue, evidence, fallback)
        if reviewed is not None:
            return reviewed
        if result.category == "conversation":
            return Observation("speech", result.basis, result.detail) if transcript else fallback
        return result

    async def classify(self, transcript, screen, sounds, fallback, *, device_context=None):
        if fallback.category == "nex":
            return fallback
        screen_result = fallback
        if screen:
            screen_result = await classify_with_screen(self.dialogue, transcript, screen, sounds, fallback)
            if screen_result is not None and screen_result.category != "unknown":
                return screen_result
        if not (transcript or sounds):
            if device_context and device_context.get("media_playing"):
                return await self.classify_device_playback(device_context, fallback)
            return screen_result

        room = await self.classify_room(transcript, sounds, fallback)
        if screen_result is None and room is not None and room.category == "unknown":
            return None
        return room

    async def classify_device_playback(self, device_context, fallback):
        messages = device_playback_messages(device_context)
        raw = await self.dialogue.background_text(messages, 48, temperature=0)
        return interpret_device_playback(raw, device_context, fallback)

    async def classify_room(self, transcript, sounds, fallback):
        messages = classifier_messages(transcript, "", sounds)
        raw = await self.dialogue.background_text(messages, 110, temperature=0)
        result = interpret(raw, transcript, "", fallback, sounds)
        if transcript and sounds and (result is None or result.category == "unknown"):
            messages = classifier_messages("", "", sounds)
            raw = await self.dialogue.background_text(messages, 110, temperature=0)
            sound = interpret(raw, "", "", fallback, sounds)
            if sound is not None and sound.category != "unknown":
                return sound
            if sound is None:
                return None
        return result

    async def consume(self, *, audio):
        last_expiration = float("-inf")
        while True:
            try:
                if not audio and monotonic() - last_expiration >= 60:
                    await asyncio.to_thread(self.jobs.expire_screens)
                    last_expiration = monotonic()
                if not audio:
                    await self.dialogue.idle.wait()
                job = await asyncio.to_thread(self.jobs.next, audio=audio, wait_for_sounds=not audio and bool(self.room))
                if job:
                    await self.analyze(job, audio)
                else:
                    await asyncio.sleep(2)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                LOG.warning("Activity recognition deferred (%s)", type(error).__name__)
                await asyncio.sleep(5)

    async def run(self):
        async with asyncio.TaskGroup() as group:
            group.create_task(self.consume(audio=False))
            if self.room:
                group.create_task(self.consume(audio=True))
