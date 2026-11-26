import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from conversation_fixtures import model
from nex.activities.brand_cues import journal_speech
from nex.activities.current import current_activity_reply, current_snapshot
from nex.activities.jobs import ActivityJobs, record_screen
from nex.activities.recall import activity_evidence, activity_question
from nex.activities.recognition import Observation, from_sounds, interpret
from nex.activities.timeline import timeline
from nex.activities.worker import ActivityWorker
from nex.assistant import Assistant
from nex.daybook import Daybook, DayContext
from nex.daybook_briefing import Briefings
from nex.recall_dates import time_key
from nex.settings import Settings
from nex.store import Store
from nex.workspace_features import update


class ActivityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), 0, 2**20)
        self.jobs = ActivityJobs(self.store)
        self.start = datetime(2026, 1, 8, 12, tzinfo=UTC)
        self.context = DayContext.parse(self.start.date().isoformat(), "UTC")

    def clip(self, start, seconds=12, *, device="iphone", identifier=None):
        key = identifier or str(uuid4())
        self.store.save_recording(key, device, time_key(start), seconds, "ambient", False, "en", b"fixture")
        return key

    def classify_sound(self, label, category):
        self.jobs.save_sounds(self.jobs.next(audio=True), [label])
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({
            "category": category, "source": "sound", "evidence": ["1"],
        })))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        asyncio.run(worker.analyze(self.jobs.next(), False))

    def test_switch_pauses_recognition_and_discards_stale_results(self):
        first = self.clip(self.start)
        job = self.jobs.next(audio=True)
        update(self.store, "activities", False)
        paused = self.clip(self.start + timedelta(seconds=12))
        self.assertIsNone(self.jobs.next(audio=True))
        self.assertEqual(timeline(self.store, self.context.bounds)["items"], [])
        update(self.store, "activities", True)
        self.jobs.save_sounds(job, ["Video game music"])
        self.assertEqual(timeline(self.store, self.context.bounds)["items"], [])
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Video game music"])
        self.assertEqual([item["id"] for item in timeline(self.store, self.context.bounds)["items"]], [first])
        self.assertIsNotNone(self.store.recording(paused))

    def test_quiet_and_speechless_media_are_analyzed_independently_of_transcription(self):
        first = self.clip(self.start)
        self.classify_sound("Video game music", "gaming")
        second = self.clip(self.start + timedelta(seconds=12))
        self.classify_sound("Video game music", "gaming")
        self.clip(self.start, identifier=first)
        quiet = self.clip(self.start + timedelta(seconds=24))
        self.jobs.save_sounds(self.jobs.next(audio=True), [])
        day = timeline(self.store, self.context.bounds)
        self.assertEqual([(p["id"], p["category"], p["count"]) for p in day["items"]],
                         [(first, "gaming", 2), (quiet, "unknown", 1)])
        self.assertEqual([p["id"] for p in timeline(self.store, self.context.bounds, source=first)["items"]],
                         [first, second])
        self.assertEqual(self.store.source(first)["audio_available"], True)
        self.assertIsNone(self.jobs.next(audio=True))
        self.assertEqual(Daybook(self.store).overview(self.context)["activities"][0]["id"], first)
        evidence = Daybook(self.store).evidence(DayContext(self.context.day, "UTC", first), "What was I doing?")
        self.assertEqual([e["id"] for e in evidence], [first])
        current = activity_evidence(self.store, "What am I doing?", "UTC", self.start + timedelta(seconds=40))
        self.assertEqual([p["id"] for p in current], [quiet])
        self.assertEqual(activity_evidence(self.store, "What am I doing?", "UTC", self.start + timedelta(hours=1)), [])

    def test_chat_snapshot_reports_each_microphone_without_reviving_stale_activity(self):
        self.clip(self.start)
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Video game music"])
        self.clip(self.start + timedelta(seconds=12), device="mac")
        self.classify_sound("Typing", "computer")
        self.clip(self.start + timedelta(seconds=24))
        self.jobs.save_sounds(self.jobs.next(audio=True), [])
        anchor = self.start + timedelta(seconds=40)
        snapshot = current_snapshot(self.store, anchor)
        self.assertEqual(snapshot["as_of"], time_key(anchor))
        self.assertEqual(snapshot["fresh_for"], 90)
        self.assertEqual({p["device"]: p["category"] for p in snapshot["items"]}, {"mac": "computer", "iphone": "unknown"})
        self.assertEqual(set(snapshot["items"][0]), {"device", "category", "title", "confidence", "started", "ended"})
        self.assertEqual(current_snapshot(self.store, self.start + timedelta(seconds=126))["items"], [])

    def test_correction_during_generation_cannot_reinstate_superseded_observation(self):
        key = self.clip(self.start)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript='I am using TikTok',state='ready' WHERE id=?", (key,))
        stale = self.jobs.next()
        self.store.correct_transcript(key, "iphone", str(uuid4()), 0, "I am using Instagram", "edited")
        self.jobs.save(stale, Observation("tiktok", "speech", "I am using TikTok"))
        self.assertEqual(timeline(self.store, self.context.bounds)["items"], [])
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps(
            {"category": "instagram", "basis": "speech", "quote": "I am using Instagram"})))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        asyncio.run(worker.analyze(self.jobs.next(), False))
        self.assertEqual(timeline(self.store, self.context.bounds)["items"][0]["category"], "instagram")
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Speech"])
        self.assertEqual(timeline(self.store, self.context.bounds)["items"][0]["category"], "instagram")
        rows, activities = Briefings(self.store, None).inputs(self.context)
        self.assertEqual(rows[0]["id"], key)
        self.assertIn("Instagram", activities[0]["text"])

    def test_screen_reports_match_capture_time_and_stop_boundaries(self):
        self.start = datetime.now(UTC) - timedelta(minutes=2)
        key = self.clip(self.start)
        capture = self.start + timedelta(seconds=2)
        record_screen(self.store, capture, "A TikTok feed is playing.")
        self.assertEqual(self.jobs.next()["screen"], "A TikTok feed is playing.")
        record_screen(self.store, self.start - timedelta(seconds=20), "Earlier window")
        self.assertEqual(self.jobs.next()["screen"], "A TikTok feed is playing.")
        record_screen(self.store, self.start + timedelta(seconds=15), None)
        after = self.clip(self.start + timedelta(seconds=20))
        self.assertEqual(self.jobs.next()["recording_id"], after)
        self.assertIsNone(self.jobs.next()["screen"])
        old = self.clip(self.start - timedelta(hours=3))
        with self.store.connect() as db:
            samples = dict(db.execute("SELECT recording_id,screen FROM activity_clips"))
        self.assertEqual(samples, {key: "A TikTok feed is playing.", after: None, old: None})

    def test_inference_requires_supported_identity_and_exact_evidence(self):
        fallback = from_sounds(["Television"], "ambient", "")
        for category, basis, quote, speech, screen in (
            ("instagram", "screen", "Instagram Reels", "", "A short video plays"),
            ("ps5", "speech", "I am playing a game", "I am playing a game", ""),
            ("tiktok", "speech", "I am using TikTok", "Check out my TikTok", ""),
        ):
            raw = json.dumps({"category": category, "basis": basis, "quote": quote})
            self.assertEqual(interpret(raw, speech, screen, fallback), fallback)
        self.assertIsNone(interpret("invalid", "", "", fallback))

    def test_worker_uses_model_classification_and_numbered_sound_evidence(self):
        for offset, sounds in enumerate((["Typing", "Computer keyboard"], ["Keyboard clicks", "Typing"]), start=1):
            self.clip(self.start + timedelta(seconds=offset * 60))
            self.jobs.save_sounds(self.jobs.next(audio=True), sounds)
            dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({
                "category": "computer", "source": "sound", "evidence": ["1", "2"],
            })))
            worker = ActivityWorker(self.store, self.store.root, None, dialogue)
            asyncio.run(worker.analyze(self.jobs.next(), False))
            messages = dialogue.background_text.await_args.args[0]
            self.assertEqual(json.loads(messages[-1]["content"]), {
                "sound_events": {"1": sounds[0], "2": sounds[1]},
            })
            properties = messages.response_format["json_schema"]["schema"]["properties"]
            self.assertEqual(properties["source"]["enum"], ["sound", "none"])
            self.assertEqual(properties["evidence"]["items"]["enum"], ["1", "2"])
            activity = timeline(self.store, self.context.bounds)["items"][-1]
            self.assertEqual((activity["title"], activity["basis"], activity["confidence"], activity["detail"]),
                             ("Using a computer", "Room audio", "Possible", f"Heard {', '.join(sounds)}."))

    def test_sound_evidence_requires_valid_references_and_preserves_model_abstention(self):
        sounds = ["Typing", "Computer keyboard"]
        fallback = from_sounds(sounds, "ambient", "")
        for category, source, evidence in (
            ("unknown", "none", []),
            ("computer", "sound", ["1", "3"]),
            ("computer", "speech", ["Typing"]),
            ("ps5", "sound", ["2"]),
        ):
            with self.subTest(category=category, source=source, evidence=evidence):
                raw = json.dumps({"category": category, "source": source, "evidence": evidence})
                self.assertEqual(interpret(raw, "", "", fallback, sounds), fallback)

    def test_youtube_requires_platform_evidence_from_the_screen_or_speech(self):
        screen = "A YouTube watch page is playing a video."
        result = json.dumps({"category": "youtube", "quote": screen})
        fallback = from_sounds(["Television"], "ambient", "")
        self.assertEqual(interpret(result, "", screen, fallback), Observation("youtube", "screen", screen))
        self.assertEqual(interpret(result, "", "", fallback, [screen]), fallback)
        self.assertEqual(interpret(result, "", "A video is playing.", fallback), fallback)
        mention = "Find our channel on YouTube after the show."
        result = json.dumps({"category": "youtube", "quote": mention})
        self.assertEqual(interpret(result, mention, "", fallback), fallback)
        self.assertEqual(journal_speech("I am watching YouTube."), "I am watching YouTube.")

    def test_generic_tv_sounds_do_not_identify_a_movie_or_show(self):
        sounds = ["Television", "Background music"]
        fallback = from_sounds(sounds, "ambient", "")
        for category in ("movie", "show"):
            sound_result = json.dumps({"category": category, "source": "sound", "evidence": ["1"]})
            quoted_sound = json.dumps({"category": category, "quote": "Television"})
            self.assertEqual(interpret(sound_result, "", "", fallback, sounds), fallback)
            self.assertEqual(interpret(quoted_sound, "See you later.", "", fallback, sounds), fallback)

    def test_worker_uses_screen_state_before_accepting_an_activity(self):
        self.clip(self.start)
        with self.store.connect() as db:
            db.execute("UPDATE activity_clips SET screen='A movie is paused.'")
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Typing", "Computer keyboard"])
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            json.dumps({"screen_state": "idle", "category": "movie", "evidence": "screen"}),
            json.dumps({"category": "computer", "source": "sound", "evidence": ["1", "2"]}),
        ]))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        asyncio.run(worker.analyze(self.jobs.next(), False))

        with_screen, without_screen = [json.loads(call.args[0][-1]["content"])
                                       for call in dialogue.background_text.await_args_list]
        self.assertEqual(with_screen["screen"], "A movie is paused.")
        self.assertEqual(without_screen, {"sound_events": {"1": "Typing", "2": "Computer keyboard"}})
        self.assertEqual(timeline(self.store, self.context.bounds)["items"][0]["category"], "computer")

    def test_model_failure_keeps_the_detected_sounds_and_defers_recognition(self):
        key = self.clip(self.start)
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Typing", "Computer keyboard"])
        dialogue = Mock(background_text=AsyncMock(return_value=""))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        asyncio.run(worker.analyze(self.jobs.next(), False))

        activity = timeline(self.store, self.context.bounds)["items"][0]
        self.assertEqual((activity["category"], activity["detail"]), ("unknown", "Heard Typing, Computer keyboard."))
        self.assertIsNone(self.jobs.next())
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute("SELECT due FROM activity_clips WHERE recording_id=?", (key,)).fetchone()[0])

    def test_audible_interface_prompts_can_identify_the_platform_without_self_narration(self):
        for category, prompt in (
            ("ps5", "Welcome back to PlayStation 5. Press the PS button to continue."),
            ("tiktok", "TikTok. For You, tab selected."),
            ("instagram", "Instagram. Reels, tab selected."),
        ):
            with self.subTest(category=category):
                fallback = from_sounds(["Speech synthesizer"], "ambient", prompt)
                self.assertEqual(fallback.category, "unknown")
                response = json.dumps({"category": category, "quote": prompt})
                self.assertEqual(interpret(response, prompt, "", fallback).category, category)
        for mention in ("Follow me on Instagram.", "How to use TikTok. For You, tab selected.",
                        "Press the PS button.", "PlayStation 5 console sale this weekend."):
            self.assertEqual(from_sounds(["Television"], "ambient", mention).category, "unknown")

    def test_current_answer_distinguishes_an_audible_console_from_the_microphone(self):
        clip = self.clip(self.start)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=? WHERE id=?",
                       ("Welcome to PlayStation 5. Press the PS button.", clip))
        dialogue = Mock(background_text=AsyncMock(return_value=json.dumps({
            "category": "ps5", "quote": "Welcome to PlayStation 5. Press the PS button.",
        })))
        worker = ActivityWorker(self.store, self.store.root, None, dialogue)
        asyncio.run(worker.analyze(self.jobs.next(), False))
        dialogue.background_text.assert_awaited_once()
        at = self.start + timedelta(seconds=14)
        reply = current_activity_reply(self.store, "Hey Nex, what am I doing right now?", "en", at)
        self.assertIn("playing on a PS5", reply["text"])
        self.assertIn("controller prompt", reply["text"])
        self.assertNotIn("iPhone", reply["text"])
        self.assertEqual(reply["sources"], [clip])
        self.assertIsNone(current_activity_reply(self.store, "What was I doing yesterday?", "en", at))
        self.assertFalse(current_activity_reply(self.store, "What am I doing?", "en", at + timedelta(hours=1))["sources"])

    def test_daybook_keeps_device_prompts_distinct_from_user_actions(self):
        menu = "Welcome back to PlayStation 5. Press the PS button to continue. Select your user to sign in."
        self.assertEqual(journal_speech(menu), "The recording contained a PlayStation 5 controller prompt.")
        self.assertEqual(journal_speech(menu.replace(". ", ", ")), journal_speech(menu))
        self.assertEqual(journal_speech(menu + " Let's meet Sam at six."),
                         "The recording contained a PlayStation 5 controller prompt. Let's meet Sam at six.")
        self.assertEqual(journal_speech("I signed in to my PS5."), "I signed in to my PS5.")
        key = self.clip(self.start)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=?,state='ready' WHERE id=?", (menu, key))
        self.jobs.save(self.jobs.next(), Observation("ps5", "speech", menu))
        summary = "At 12:00 UTC, the recording picked up a PlayStation 5 controller prompt."
        generate = AsyncMock(return_value=json.dumps({"summary": summary, "highlights": [
            {"kind": "key_moment", "source": key, "text": "A controller prompt was audible."},
        ]}))
        reports = Briefings(self.store, generate)
        report = asyncio.run(reports.build(*reports.inputs(self.context)))
        self.assertEqual(report["summary"], summary)
        self.assertEqual(report["highlights"][0]["kind"], "key_moment")
        self.assertEqual(report["highlights"][0]["source"], key)
        generate.assert_awaited_once()
        other = self.clip(self.start + timedelta(seconds=20))
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=?,state='ready' WHERE id=?", ("Let's meet Sam at six.", other))
        asyncio.run(reports.build(*reports.inputs(self.context)))
        evidence = generate.await_args.args[0][1]["content"]
        self.assertIn("Sam at six", evidence)
        self.assertIn("Select your user", evidence)

    def test_upgrade_seeds_existing_archive_and_restart_preserves_completed_work(self):
        key = self.clip(self.start)
        with self.store.connect() as db:
            db.executescript("DROP TRIGGER activity_new_recording; DROP TRIGGER activity_transcript_changed; "
                             "DROP TABLE activity_clips; DROP TABLE activity_screen_samples;")
        upgraded = Store(self.store.root, 0, 2**20)
        jobs = ActivityJobs(upgraded)
        self.assertEqual(jobs.next(audio=True)["recording_id"], key)
        jobs.save_sounds(jobs.next(audio=True), ["Video game music"])
        reopened = Store(self.store.root, 0, 2**20)
        self.assertIsNone(ActivityJobs(reopened).next(audio=True))
        self.assertEqual(timeline(reopened, self.context.bounds)["items"][0]["category"], "unknown")
        self.assertEqual(ActivityJobs(reopened).next()["recording_id"], key)

    def test_upgrade_requeues_only_unclear_clips_with_sound_evidence_once(self):
        known = self.clip(self.start)
        self.classify_sound("Video game music", "gaming")
        self.clip(self.start + timedelta(seconds=30))
        self.jobs.save_sounds(self.jobs.next(audio=True), [])
        mixed = self.clip(self.start + timedelta(seconds=45))
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript='Hi there' WHERE id=?", (mixed,))
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Speech"])
        self.jobs.save(self.jobs.next(), from_sounds(["Speech"], "ambient", "Hi there"))
        unclear = self.clip(self.start + timedelta(seconds=60))
        sounds = ["Typing", "Computer keyboard"]
        self.jobs.save_sounds(self.jobs.next(audio=True), sounds)
        self.jobs.save(self.jobs.next(), from_sounds(sounds, "ambient", ""))
        self.assertIsNone(self.jobs.next())
        with self.store.connect() as db:
            db.execute("DROP TABLE activity_migrations")

        upgraded = Store(self.store.root, 0, 2**20)
        jobs = ActivityJobs(upgraded)
        self.assertEqual(jobs.next()["recording_id"], unclear)
        self.assertIsNone(jobs.next(audio=True))
        self.assertEqual([(item["id"], item["category"]) for item in timeline(upgraded, self.context.bounds)["items"]
                          if item["id"] in (known, unclear)], [(known, "gaming"), (unclear, "unknown")])
        jobs.save(jobs.next(), from_sounds(sounds, "ambient", ""))
        reopened = Store(self.store.root, 0, 2**20)
        self.assertIsNone(ActivityJobs(reopened).next())

    def test_cross_midnight_and_historical_questions_use_recording_time(self):
        start = datetime(2026, 4, 20, 6, 59, 55, tzinfo=UTC)
        key = self.clip(start, 12)
        self.classify_sound("Video game music", "gaming")
        scope = DayContext.parse("2026-04-20", "America/Vancouver")
        day = timeline(self.store, scope.bounds)
        self.assertEqual(day["items"][0]["started"], scope.bounds[0])
        ask = start + timedelta(days=1, hours=1)
        self.clip(ask - timedelta(seconds=14), device="mac")
        self.classify_sound("Typing", "computer")
        self.clip(ask - timedelta(seconds=1), device="mac")
        self.jobs.save_sounds(self.jobs.next(audio=True), ["Television"])
        settings = Settings(self.store.root)
        engines = Mock()
        engines.generate = model(lambda data: [{"name": "activities", "mode": "range",
                                                "start_at": "2026-04-20T00:00", "end_at": "2026-04-20T00:01"}]
                                 if "yesterday" in data["question"] else [{"name": "activities", "mode": "current"}],
                                 compose="Game music was audible.")
        assistant = Assistant(self.store, engines, settings)
        result = assistant.answer("What was I playing yesterday at 12:00 am?", "en", zone=scope.zone, asked_at=ask)
        self.assertEqual(result["sources"], [key])
        self.assertIn("Video game music", engines.generate.call_args.args[0][-1]["content"])
        assistant.answer("Suggest a short break", "en", zone=scope.zone, asked_at=ask, spoken=True)
        self.assertIn("Typing", engines.generate.call_args.args[0][-1]["content"])
        self.assertFalse(activity_question("Which platforms can I play this game on?"))
        self.assertFalse(activity_question("What should I do next?"))


if __name__ == "__main__":
    unittest.main()
