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
from ..models import Engines, ModelError
from ..recall_dates import time_key
from ..settings import Settings
from .jobs import ActivityJobs
from .recognition import classifier_messages, from_sounds, interpret
from .screen_recognition import classify_with_screen

LOG = logging.getLogger(__name__)


def retry_time():
    return time_key(datetime.now(UTC) + timedelta(minutes=5))


class ActivityWorker:
    def __init__(self, store, runtime, room, dialogue, *, settings_root=None):
        self.jobs = ActivityJobs(store)
        self.audio = AudioFiles(store.root / "audio")
        self.settings_root, self.runtime = settings_root or store.root, runtime
        self.room, self.dialogue = room, dialogue

    def listen(self, job):
        # Read the archive service's current decoder configuration, including changes made since launch.
        settings = Settings(self.settings_root)
        decoder = Engines(settings, self.runtime)
        with TemporaryDirectory(dir=self.runtime) as directory:
            path = decoder.decode(self.audio.path(job["recording_id"]), Path(directory), settings.read())
            with wave.open(str(path), "rb") as stream:
                return self.room.sound_labels(stream.readframes(stream.getnframes()))

    async def analyze(self, job, audio):
        if audio:
            try:
                sounds = await asyncio.to_thread(self.listen, job)
            except (ModelError, OSError, wave.Error, RuntimeError, ValueError) as error:
                await asyncio.to_thread(self.jobs.defer_audio, job, retry_time())
                LOG.warning("Activity audio deferred (%s)", type(error).__name__)
            else:
                await asyncio.to_thread(self.jobs.save_sounds, job, sounds)
            return
        sounds = json.loads(job["sounds"]) if job["sounds"] is not None else None
        transcript, screen = (job["transcript"] or "")[:1500], job["screen"] or ""
        fallback = from_sounds(sounds, job["mode"], transcript)
        result = await self.classify(transcript, screen, sounds, fallback)
        await asyncio.to_thread(self.jobs.save, job, result or fallback, due=None if result else retry_time())

    async def classify(self, transcript, screen, sounds, fallback):
        if fallback.category == "nex":
            return fallback
        if screen:
            result, reconsider = await classify_with_screen(self.dialogue, transcript, screen, sounds, fallback)
            if not reconsider:
                return result
        if not (transcript or sounds):
            return fallback
        messages = classifier_messages(transcript, "", sounds)
        raw = await self.dialogue.background_text(messages, 110, temperature=None if transcript else 0)
        return interpret(raw, transcript, "", fallback, sounds)

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
