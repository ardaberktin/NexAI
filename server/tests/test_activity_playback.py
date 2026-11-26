import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from nex.activities.current import current_activity_reply
from nex.activities.jobs import ActivityJobs, record_device_context
from nex.activities.recognition import SOUND_CATEGORIES, Observation, from_sounds, interpret
from nex.activities.timeline import timeline
from nex.activities.voice_origin import review_voice_origin, source_evidence, source_observation
from nex.activities.worker import ActivityWorker
from nex.daybook import DayContext
from nex.http_api import recorder_operation
from nex.live import INPUT_RATE
from nex.recall_dates import time_key
from nex.store import Store


class PlaybackReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), 0, 2**20)
        self.jobs = ActivityJobs(self.store)
        self.start = datetime(2026, 1, 8, 12, tzinfo=UTC)
        self.context = DayContext.parse(self.start.date().isoformat(), "UTC")

    def clip(self, start, seconds=12, *, device="iphone", media_playing=False):
        key = str(uuid4())
        self.store.save_recording(
            key, device, time_key(start), seconds, "ambient", False, "en", b"fixture",
            media_playing=media_playing,
        )
        return key

    async def test_confirmed_pause_excludes_the_earlier_playback_cue(self):
        transcript = "Pause the video, please. Okay, it's paused. What did you want to ask?"
        sounds = ["Speech", "Conversation"]
        evidence = source_evidence(transcript, sounds, [{"seconds_before": 3, "labels": ["Television"]}])
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            '{"stopped":true}', '{"origin":"room_conversation","evidence":"words"}',
        ]))

        result = await review_voice_origin(dialogue, evidence, from_sounds(sounds, "ambient", transcript))

        classification = dialogue.background_text.await_args.args[0]
        self.assertEqual(json.loads(classification[-1]["content"])["earlier_playback"], [])
        self.assertEqual(result, Observation("conversation", "speech", transcript))

    async def test_model_can_connect_current_dialogue_with_earlier_playback(self):
        transcript = "Didn't that happen last week? I thought it was later."
        sounds = ["Speech", "Conversation"]
        evidence = source_evidence(transcript, sounds, [{"seconds_before": 3, "labels": ["Television"]}])
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            '{"stopped":false}', '{"continuity":"continues"}',
        ]))

        result = await review_voice_origin(dialogue, evidence, from_sounds(sounds, "ambient", transcript))

        self.assertEqual(result, Observation("media", "sound", "Heard Speech, Conversation. Nearby recordings picked up Television."))

    def test_sound_only_classification_never_accepts_conversation_or_livestream(self):
        self.assertNotIn("conversation", SOUND_CATEGORIES)
        self.assertNotIn("livestream", SOUND_CATEGORIES)
        sounds = ["Speech", "Conversation"]
        fallback = from_sounds(sounds, "ambient", "")
        raw = json.dumps({"category": "conversation", "source": "sound", "evidence": ["2"]})
        self.assertEqual(interpret(raw, "", "", fallback, sounds), fallback)

    async def test_failed_voice_origin_review_downgrades_stage_one_conversation(self):
        key = self.clip(self.start)
        transcript = "Wait did you see that play? Yeah that was wild."
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=?,state='ready' WHERE id=?", (transcript, key))
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Speech", "Conversation"])
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            json.dumps({"category": "conversation", "quote": transcript}),
            "",
        ]))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)

        await worker.analyze(self.jobs.next(), False)

        item = timeline(self.store, self.context.bounds)["items"][0]
        self.assertEqual(item["category"], "speech")
        self.assertEqual(item["title"], "Speech nearby")

    async def test_device_playback_and_audio_profile_reach_voice_origin_review(self):
        now = datetime.now(UTC) - timedelta(seconds=30)
        key = self.clip(now, device="iphone", media_playing=True)
        transcript = "Chat let me know if the audio sounds good today."
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=?,state='ready' WHERE id=?", (transcript, key))
        profile = {"speaker_count": 1, "speaker_turns": 0, "speech_ratio": 0.9, "owner_speaking": False}
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Speech", "Conversation", "Laughter"], audio_context=profile)
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            json.dumps({"category": "conversation", "quote": transcript}),
            json.dumps({"origin": "recorded_playback", "evidence": "device_playback"}),
        ]))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)

        await worker.analyze(self.jobs.next(), False)

        review_input = json.loads(dialogue.background_text.await_args_list[1].args[0][-1]["content"])
        self.assertEqual(review_input["audio_profile"], profile)
        self.assertEqual(review_input["current_sounds"], ["Speech", "Conversation", "Laughter"])
        self.assertEqual(review_input["device_playback"], {"media_playing": True, "device": "iphone"})
        self.assertEqual(review_input["recording_device"], "iphone")
        item = timeline(self.store, (time_key(now - timedelta(minutes=5)), time_key(now + timedelta(minutes=5))))["items"][0]
        self.assertEqual((item["category"], item["basis"], item["confidence"]), ("media", "Device playback", "Likely"))
        reply = current_activity_reply(self.store, "What am I doing right now?", "en", now + timedelta(seconds=15))
        self.assertIn("Active playback on your device", reply["text"])

    async def test_mac_device_playback_attaches_and_classifies_silent_headphones_clip(self):
        now = datetime.now(UTC) - timedelta(seconds=25)
        record_device_context(self.store, "mac", now - timedelta(seconds=5), "Google Chrome", True)
        key = self.clip(now, device="iphone")
        self.jobs.save_sounds(self.jobs.next(audio=True), [])
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({"category": "media"})))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)

        await worker.analyze(self.jobs.next(), False)

        item = timeline(self.store, (time_key(now - timedelta(minutes=5)), time_key(now + timedelta(minutes=5))),
                        source=key)["items"][0]
        self.assertEqual((item["category"], item["basis"]), ("media", "Device playback"))
        self.assertIn("Google Chrome", item["detail"])

        record_device_context(self.store, "mac", now + timedelta(seconds=15), None, False)
        after = self.clip(now + timedelta(seconds=20), device="iphone")
        self.assertIsNone(self.jobs.next()["device_context"])
        self.assertEqual(self.jobs.next()["recording_id"], after)

    async def test_earlier_playback_sound_requeues_following_ambiguous_clip(self):
        earlier = self.clip(self.start)
        later = self.clip(self.start + timedelta(seconds=14))
        later_audio = self.jobs.next(audio=True)
        self.assertEqual(later_audio["recording_id"], later)
        self.jobs.save_sounds(later_audio, ["Speech"])
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            json.dumps({"category": "speech", "source": "sound", "evidence": ["1"]}),
            json.dumps({"origin": "uncertain_speech", "evidence": "current_sounds"}),
        ]))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        await worker.analyze(self.jobs.next(wait_for_sounds=True), False)
        self.assertIsNone(self.jobs.next(wait_for_sounds=True))

        earlier_audio = self.jobs.next(audio=True)
        self.assertEqual(earlier_audio["recording_id"], earlier)
        self.jobs.save_sounds(earlier_audio, ["Television"])

        requeued = self.jobs.next(wait_for_sounds=True)
        self.assertEqual(requeued["recording_id"], later)

    def test_source_observation_supports_recent_activity_and_audio_profile(self):
        recent = [{"seconds_before": 6, "category": "ps5", "title": "Playing PS5", "sounds": ["Video game music"]}]
        evidence = source_evidence(
            "Watch out behind you!", ["Speech", "Conversation"], [],
            audio_context={"speaker_count": 2, "speaker_turns": 1, "speech_ratio": 0.9, "owner_speaking": False},
            recent=recent,
        )
        fallback = from_sounds(["Speech", "Conversation"], "ambient", "Watch out behind you!")
        continued = source_observation(
            json.dumps({"origin": "recorded_playback", "evidence": "recent_activities"}), evidence, fallback,
        )
        self.assertEqual(continued.category, "ps5")
        self.assertIn("playing ps5", continued.detail.lower())
        speech = source_observation(json.dumps({"origin": "uncertain_speech", "evidence": "audio_profile"}), evidence, fallback)
        self.assertEqual(speech, Observation("speech", "sound", "Heard Speech, Conversation."))
        self.assertTrue(recorder_operation("PUT", "/v1/context/device"))

    async def test_ps5_and_tv_continuity_and_open_ended_activities(self):
        first = self.clip(self.start)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=?,state='ready' WHERE id=?",
                       ("Welcome back to PlayStation 5. Press the PS button.", first))
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Speech", "Video game music"])
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({
            "category": "ps5", "quote": "Welcome back to PlayStation 5.",
        })))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        await worker.analyze(self.jobs.next(wait_for_sounds=True), False)

        # Next clip is an in-game cutscene without repeating "PlayStation 5".
        second = self.clip(self.start + timedelta(seconds=14))
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=?,state='ready' WHERE id=?",
                       ("Watch out behind you! Reloading now.", second))
        self.jobs.save_sounds(
            self.jobs.next(audio=True), ["Speech", "Conversation"],
            audio_context={"speaker_count": 2, "speaker_turns": 1, "speech_ratio": 0.85, "owner_speaking": False},
        )
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({
            "category": "ps5", "quote": "Watch out behind you! Reloading now.",
        })))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        await worker.analyze(self.jobs.next(wait_for_sounds=True), False)
        supplied = json.loads(dialogue.background_text.await_args.args[0][-1]["content"])
        self.assertEqual(supplied["recent_activities"][0]["category"], "ps5")
        self.assertEqual(timeline(self.store, self.context.bounds, source=second)["items"][-1]["category"], "ps5")

        # Watching TV from room audio.
        tv_clip = self.clip(self.start + timedelta(minutes=5))
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Television", "Music"])
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({
            "category": "tv", "source": "sound", "evidence": ["1"],
        })))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        await worker.analyze(self.jobs.next(wait_for_sounds=True), False)
        tv_item = timeline(self.store, self.context.bounds, source=tv_clip)["items"][-1]
        self.assertEqual((tv_item["category"], tv_item["title"]), ("tv", "Watching TV"))

        # Open-ended activity outside fixed categories.
        guitar_clip = self.clip(self.start + timedelta(minutes=10))
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Guitar", "Acoustic guitar"])
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({
            "category": "other", "title": "Playing guitar", "source": "sound", "evidence": ["1", "2"],
        })))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        await worker.analyze(self.jobs.next(wait_for_sounds=True), False)
        guitar_item = timeline(self.store, self.context.bounds, source=guitar_clip)["items"][-1]
        self.assertEqual((guitar_item["category"], guitar_item["title"]), ("other", "Playing guitar"))
        reply = current_activity_reply(
            self.store, "What am I doing right now?", "en", self.start + timedelta(minutes=10, seconds=14),
        )
        self.assertIn("playing guitar", reply["text"])

    @unittest.skipUnless(
        all(importlib.util.find_spec(module) for module in ("cryptography", "numpy")),
        "needs the live voice dependencies",
    )
    def test_voice_profiles_owner_present_checks_speaker_clusters(self):
        import numpy as np

        from nex.live.voice_profiles import VoiceProfiles

        owner_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        other_vec = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        room = SimpleNamespace(lock=unittest.mock.MagicMock(), voice=lambda arr: owner_vec if arr[0] > 0 else other_vec)
        profiles = VoiceProfiles(self.store.privacy, room, "test-secret")
        encrypted = profiles.cipher.encrypt(json.dumps(owner_vec.tolist()).encode())
        with self.store.privacy.transaction() as db:
            db.execute("INSERT INTO voices(id,label,template,created) VALUES('v1','Owner',?,'2026-01-01T00:00:00Z')",
                       (encrypted,))
        samples = np.concatenate([
            np.full(INPUT_RATE * 2, -0.5, dtype=np.float32),
            np.full(INPUT_RATE * 2, 0.5, dtype=np.float32),
        ])
        other_only = [SimpleNamespace(speaker=0, start=0.0, end=2.0)]
        both = [SimpleNamespace(speaker=0, start=0.0, end=2.0), SimpleNamespace(speaker=1, start=2.0, end=4.0)]
        self.assertFalse(profiles.owner_present(samples, other_only))
        self.assertTrue(profiles.owner_present(samples, both))

    def test_upgrade_migrates_legacy_activity_clips_table(self):
        key = self.clip(self.start)
        with self.store.connect() as db:
            db.executescript(
                """DROP TRIGGER activity_new_recording;
                   DROP TRIGGER activity_transcript_changed;
                   DROP TABLE activity_clips;
                   DROP TABLE activity_device_samples;
                   CREATE TABLE activity_clips (
                       recording_id TEXT PRIMARY KEY, started TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                       due TEXT DEFAULT '', sound_due TEXT DEFAULT '', sounds TEXT, screen TEXT,
                       category TEXT, basis TEXT, detail TEXT
                   );"""
            )
            db.execute("INSERT INTO activity_clips(recording_id, started) VALUES(?, ?)", (key, time_key(self.start)))
        upgraded = Store(self.store.root, 0, 2**20)
        with upgraded.connect() as db:
            cols = {col["name"] for col in db.execute("PRAGMA table_info(activity_clips)")}
            self.assertTrue({"title", "audio_context", "device_context", "clip_media_playing"}.issubset(cols))
