import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from conversation_fixtures import model
from nex.assistant import Assistant
from nex.live_memory import LiveMemory
from nex.models import Engines
from nex.passages import matching_passage
from nex.recall_dates import recall_dates
from nex.response_language import reply_language
from nex.settings import Settings
from nex.store import ArchiveFull, RecordingConflict, Store, TranscriptConflict, now
from nex.transcription import has_audible_input, whisper_text
from nex.wake import addressed_question, continued_question
from nex.web_html import Page
from nex.worker import Worker


class NexArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(self.root)
        self.store = Store(self.root, 0, 1_048_576)
        self.engines = Engines(self.settings, self.root / "runtime")
        self.assistant = Assistant(self.store, self.engines, self.settings)
        self.worker = Worker(self.store, self.engines, self.assistant, self.settings)

    def clip(self, identifier, body=b"test audio", kind="ambient", wake=False, started=None, zone="UTC"):
        return self.store.save_recording(
            identifier, "mac", started or now(), 2.25, kind, wake, "auto", body, zone=zone,
        )

    def transcribe(self, identifier, text):
        self.assertTrue(self.worker.local_transcript(identifier, "mac", text))
        row = self.worker.claim()
        self.assertEqual(row["id"], identifier)
        self.worker.process(row)
        return self.store.recording(identifier)

    def test_live_result_is_archived_once_in_both_arrival_orders(self):
        live = LiveMemory(self.root)
        for device, early in product(("mac", "watch"), (False, True)):
            identifier = str(uuid4())
            started = now()
            answer = {"text": "Tuesday.", "sources": [], "engine": "local_model", "language": "en"}
            live.save_result(identifier, device, transcript="When did we schedule the test?")
            if early:
                live.save_result(identifier, device, reply=answer)
            args = (identifier, device, started, 1.5, "voice", False, "en", b"synthetic")
            self.assertTrue(self.store.save_recording(*args, live_handled=True)[1])
            with patch.object(self.assistant, "answer") as generate:
                self.worker.process(self.worker.claim())
                generate.assert_not_called()
            if not early:
                live.save_result(identifier, device, reply=answer)
            self.assertEqual(self.store.recording(identifier)["reply"], answer)
            self.assertEqual(self.store.source("a-" + identifier)["sources"], [identifier])
            self.assertFalse(self.store.save_recording(*args, live_handled=True)[1])
            self.assertFalse(self.store.ready_replies(device, "0"))
            self.store.correct_transcript(identifier, device, str(uuid4()), 0, "Corrected.", "Corrected.")
            live.save_result(identifier, device, transcript="An outdated transcript.")
            self.assertEqual(self.store.recording(identifier)["transcript"], "Corrected.")

    def test_retained_and_idempotent_after_reopening(self):
        identifier, started = str(uuid4()), now()
        first, created = self.clip(identifier, started=started)
        self.assertTrue(created)
        self.store = Store(self.root, 0, 1_048_576)
        row, created = self.clip(identifier, started=started)
        self.assertFalse(created)
        self.assertEqual(first, row)
        self.assertEqual(self.store.audio_path(identifier).read_bytes(), b"test audio")
        self.assertEqual(self.store.status()["audio_bytes"], 10)
        with self.assertRaises(RecordingConflict):
            self.clip(identifier, b"different", started=started)
        with self.assertRaises(ArchiveFull):
            self.clip(str(uuid4()), b"x" * 1_048_576)
        self.assertEqual(self.store.status()["audio_bytes"], 10)

    def test_transcript_and_notes_stay_quiet_with_clickable_recall(self):
        identifier = str(uuid4())
        self.clip(identifier, wake=True)
        self.worker.process(self.worker.claim())
        self.assertEqual(self.store.recording(identifier)["state"], "awaiting_transcription")
        transcript = "Yedekleme için cuma günü karar verdik. The archive lives in the vault."
        with patch.object(self.engines, "generate", return_value="Yedekleme cuma günü.") as generate:
            completed = self.transcribe(identifier, transcript)
        self.assertEqual(generate.call_args.args[0][-1]["content"], transcript)
        self.assertIsNone(completed["reply"])
        note = self.store.source("n-" + identifier)
        self.assertEqual(note["sources"], [identifier])
        self.assertEqual((note["text"], note["engine"]), ("Yedekleme cuma günü.", "local_model"))
        self.assistant.generate_conversation = model([{"name":"archive", "query":"Yedekleme"}])
        answer = self.assistant.answer("Yedekleme hangi gün?", "tr")
        self.assertEqual(answer["language"], "tr")
        self.assertEqual(answer["sources"], [identifier])
        self.assertIn("cuma", answer["text"])

    def test_unrecognized_voice_keeps_audio_without_creating_recall_or_an_answer(self):
        identifier = str(uuid4())
        self.clip(identifier, kind="voice")
        with patch.object(self.assistant, "answer") as generate:
            result = self.transcribe(identifier, "( ˘ω˘ )")
            generate.assert_not_called()
        self.assertEqual(result["state"], "unrecognized")
        self.assertIsNone(result["reply"])
        self.assertTrue(result["audio_available"])
        self.assertFalse(self.store.search(""))
        self.assertTrue(self.store.source(identifier)["audio_available"])
        event, = self.worker.voice_streams.events(identifier)
        self.assertEqual(event["state"], "unrecognized")

    def test_duplicate_note_is_hidden_from_search_but_its_link_still_works(self):
        identifier, started = str(uuid4()), now()
        note = "We checked the engine."
        with self.store.connect() as db:
            self.store.add_memory(db, identifier, started, "recording", "mac", note)
            self.store.add_memory(db, "n-" + identifier, started, "note", "mac", note, [identifier])
        for query in ("", "engine"):
            self.assertEqual([entry["id"] for entry in self.store.search(query)], [identifier])
        self.assertEqual(self.store.source("n-" + identifier)["sources"], [identifier])
        with self.store.connect() as db:
            self.store.add_memory(db, "n-" + identifier, started, "note", "mac", "Engine checked.", [identifier])
        self.assertEqual(len(self.store.search("engine")), 2)

    def test_whisper_suppresses_unreliable_segments_and_keeps_a_clear_short_turn(self):
        examples = [
            ("( ˘ω˘ )", [("ω", 0.96)], ""),
            ("P is wait", [("P", 0.01), ("is", 0.001), ("wait", 0.001)], ""),
            ("Hello, how are you?", [("Hello", 0.78), ("how", 0.97), ("are", 1.0), ("you", 1.0)],
             "Hello, how are you?"),
            ("İyiyim.", [("İyi", 0.78), ("yim", 0.84)], "İyiyim."),
        ]
        for text, tokens, expected in examples:
            with self.subTest(text=text):
                self.assertEqual(whisper_text({"transcription": [{"text": text, "tokens": [
                    {"text": word, "p": score} for word, score in tokens
                ]}]}), expected)
        self.assertFalse(has_audible_input([0] * 800 + [300] + [0] * 1599, spoken=True))
        self.assertTrue(has_audible_input([350, -350] * 850, spoken=True))

    def test_voice_and_recent_addressed_question(self):
        self.assistant.generate_conversation = model([], compose="I can help with that.", recorded=lambda data: {
            "candidate": 1, "quote": data["candidates"][0]["transcript"],
        })
        for kind, wake, text in [
            ("voice", False, "Where is the journal?"),
            ("ambient", True, "Hey Nex, where is the journal?"),
            ("ambient", True, "Hey Nex, set a reminder to call me tomorrow."),
        ]:
            identifier = str(uuid4())
            self.clip(identifier, kind=kind, wake=wake)
            result = self.transcribe(identifier, text)
            self.assertIsNotNone(result["reply"])
        self.assertIsNone(addressed_question("Hey Nex, make a note. Where is the journal?"))
        stale = str(uuid4())
        started = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        self.clip(stale, wake=True, started=started)
        self.assertIsNone(self.transcribe(stale, "Hey Nex, where is the journal?")["reply"])

    def test_recorded_voice_greeting_replies_without_archive_context(self):
        self.assistant.generate_conversation = model([], compose="Hey!")
        with patch.object(self.store, "search") as search, patch.object(self.engines, "generate") as generate:
            result = self.assistant.answer("I said hello.", "auto", spoken=True)
        self.assertEqual(result["text"], "Hey!")
        self.assertEqual(result["sources"], [])
        search.assert_not_called()
        generate.assert_not_called()

    def test_auto_reply_language_distinguishes_dotted_and_dotless_i(self):
        self.assertEqual(reply_language("What is 7 + 5?", "auto"), "en")
        self.assertEqual(reply_language("7 ile 5'in toplamı kaçtır.", "auto"), "tr")
        self.assertEqual(reply_language("What is 7 + 5?", "tr"), "tr")

    def test_standalone_facts_ignore_matching_recordings_and_prior_answers(self):
        with self.store.connect() as db:
            self.store.add_memory(db, str(uuid4()), now(), "recording", "mac",
                                  "Hey Nex, give me three facts about cats.")
            self.store.add_memory(db, "a-" + str(uuid4()), now(), "answer", "mac",
                                  "The archive doesn't include information about cats.")
        page = Page("Cat behaviour", "https://example.org/cats", "Cats use their whiskers to sense nearby objects.")
        for question, language, terms in [
            ("Give me three facts about cats.", "en", "cats"),
            ("Kediler hakkında üç bilgi ver.", "tr", "Kediler"),
        ]:
            with (
                self.subTest(language=language),
                patch.object(self.store, "search", wraps=self.store.search) as search,
                patch("nex.web_answers.lookup", return_value=[page]) as lookup,
                patch.object(self.engines, "generate", model([{"name":"web","query":terms}], compose="Generated [1]",
                                                           language=language)) as generate,
            ):
                result = self.assistant.answer(question, "auto", spoken=True)
                search.assert_not_called()
                self.assertEqual(lookup.call_args.args[0].terms, terms)
                self.assertEqual([self.store.source(i)["kind"] for i in result["sources"]], ["web"])
                self.assertEqual(result["language"], language)
                messages = generate.call_args.args[0]
                prompt = json.loads(messages[-1]["content"])
                self.assertEqual(prompt["question"], question)
                self.assertNotIn("recent", prompt)

    def test_explicit_archive_request_takes_priority_over_general_fact_phrasing(self):
        identifier = str(uuid4())
        with self.store.connect() as db:
            self.store.add_memory(db, identifier, now(), "recording", "mac",
                                  "We agreed to start the cat shelter fundraiser on Tuesday.")
        self.assistant.generate_conversation = model([{"name":"archive","query":"cat shelter"}])
        result = self.assistant.answer("Give me facts about the cat shelter from my recordings.", "en")
        self.assertEqual(result["sources"], [identifier])
        self.assertIn("Tuesday", result["text"])

    def test_wake_phrase_must_address_nex(self):
        self.assertIsNone(addressed_question("Nex handles the archive, right?"))
        self.assertIsNone(addressed_question("I told him to say Hey Nex, what time is it?"))
        self.assertEqual(addressed_question("The session is over. Selam Nex, yedekler nerede?"),
                         "yedekler nerede?")
        self.assertEqual(addressed_question("Hey, Nex. What is 7 + 5?"), "What is 7 + 5?")
        self.assertEqual(addressed_question("Merhaba Nex. 7 ile 5'in toplamı kaçtır."),
                         "7 ile 5'in toplamı kaçtır.")
        self.assertIsNone(continued_question("Hey Nex, make a note.", "Where is the journal?"))
        self.assertIsNone(continued_question("Hey Nex, where is the journal?", "Can you repeat it?"))
        self.assertEqual(continued_question("Hey.", "Nex, where is the journal?"), "where is the journal?")
        self.assertEqual(continued_question("Hey Nex, when will the.", "archive be ready?"),
                         "when will the archive be ready?")

    def test_adjacent_wake_phrase_is_answered_once_in_either_upload_order(self):
        from nex.coding.sandbox import Run

        self.assistant.generate_conversation = model([{"name":"python"}], code="print(6 * 7)", recorded=lambda data: {
            "candidate": 1, "quote": "What is six times seven?",
        })
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                start = datetime.now(UTC) - timedelta(seconds=20 if reverse else 10)
                first, second = str(uuid4()), str(uuid4())
                clips = [(first, start, "Hey Nex."),
                         (second, start + timedelta(seconds=2.25), "What is six times seven?")]
                with patch("nex.coding.answers.run_python", return_value=Run("ok", "42")) as run:
                    for identifier, timestamp, transcript in reversed(clips) if reverse else clips:
                        self.clip(identifier, wake=True, started=timestamp.isoformat())
                        self.transcribe(identifier, transcript)
                    run.assert_awaited_once()
                self.assertIsNone(self.store.recording(first)["reply"])
                self.assertEqual(self.store.recording(second)["reply"]["engine"], "python_sandbox")
                self.assertEqual(self.store.source("a-" + second)["sources"][:2], [first, second])
                self.assertIsNone(self.store.source("a-" + first))

    def test_split_wake_phrase_does_not_cross_a_recording_gap_or_manual_edit(self):
        start = datetime.now(UTC) - timedelta(seconds=10)
        first, after_gap = str(uuid4()), str(uuid4())
        self.clip(first, wake=True, started=start.isoformat())
        self.transcribe(first, "Hey Nex.")
        self.clip(after_gap, wake=True, started=(start + timedelta(seconds=4)).isoformat())
        self.assertIsNone(self.transcribe(after_gap, "Where is the journal?")["reply"])

        edited, after_edit = str(uuid4()), str(uuid4())
        start -= timedelta(seconds=20)
        self.clip(edited, wake=True, started=start.isoformat())
        self.transcribe(edited, "Nothing to ask.")
        self.store.correct_transcript(edited, "mac", str(uuid4()), 0, "Hey Nex.", "Hey Nex.")
        self.clip(after_edit, wake=True, started=(start + timedelta(seconds=2.25)).isoformat())
        self.assertIsNone(self.transcribe(after_edit, "Where is the journal?")["reply"])

    def test_recall_uses_passage_at_recording_end(self):
        identifier = str(uuid4())
        filler = "Ayrıntıları sonra konuşuruz. " * 40
        fact = "Şifreli yedekler salı günü hazır olacak."
        self.clip(identifier)
        self.transcribe(identifier, filler + fact)
        self.assistant.generate_conversation = model([{"name":"archive","query":"Şifreli yedekler"}])
        answer = self.assistant.answer("Şifreli yedekler ne zaman?", "tr")
        self.assertEqual(answer["sources"], [identifier])
        self.assertIn(fact, answer["text"])
        self.assertLessEqual(len(matching_passage(filler + fact, "Şifreli yedekler")), 460)

    def test_recall_with_inflected_cross_language_keywords(self):
        identifier = str(uuid4())
        self.clip(identifier)
        self.transcribe(identifier, "Şifreli yedekleri salı günü dış diske kopyalayacağız.")
        config = self.settings.read() | {"llama_server": "fixture"}
        with patch.object(self.settings, "read", return_value=config), patch.object(
            self.engines, "generate",
            new=model([{"name":"archive","query":"şifreleme yedeklemeler kopyalanacak harici sürücü"}], compose="The backup was scheduled for Tuesday."),
        ):
            result = self.assistant.answer("When will the encrypted backups be copied?", "en")
        self.assertEqual(result["sources"], [identifier])
        self.assertIn("Tuesday", result["text"])

    def test_last_month_recall_uses_calendar_boundaries_in_device_time_zone(self):
        october, november = str(uuid4()), str(uuid4())
        with self.store.connect() as db:
            self.store.add_memory(db, october, "2026-11-01T06:59:59.000+00:00", "recording", "mac",
                                  "October backup decision: use the blue drive. Mavi diskte yedekler var.")
            self.store.add_memory(db, november, "2026-11-01T07:00:00.000+00:00", "recording", "mac",
                                  "November backup decision: use the red drive. Kırmızı diskte yedekler var.")
        reference = datetime.fromisoformat("2026-11-16T23:00:00+00:00")
        keywords = iter(["backups", "yedekler", "", "helicopters"])
        self.assistant.generate_conversation = model(lambda _: [{"name": "archive", "query": next(keywords), "date":"last month"}])
        context = {"zone": "America/Vancouver", "asked_at": reference}
        self.assertEqual(self.assistant.answer("What did we decide about backups last month?", "en",
                                              **context)["sources"], [october])
        self.assertEqual(self.assistant.answer("Geçen ay yedekler neredeydi?", "tr",
                                              **context)["sources"], [october])
        self.assertEqual(self.assistant.answer("What did we talk about last month?", "en",
                                              **context)["sources"], [october])
        self.assertEqual(self.assistant.answer("When did we discuss helicopters last month?", "en",
                                              **context)["sources"], [])
        self.assertEqual(recall_dates("2026-11-01", "America/Los_Angeles").bounds, (
            "2026-11-01T07:00:00.000+00:00", "2026-11-02T08:00:00.000+00:00",
        ))

    def test_queued_voice_question_uses_recording_date_and_zone(self):
        december, january = str(uuid4()), str(uuid4())
        with self.store.connect() as db:
            self.store.add_memory(db, december, "2025-12-12T18:00:00.000+00:00", "recording", "mac",
                                  "December backups were moved to the rack.")
            self.store.add_memory(db, january, "2026-01-12T18:00:00.000+00:00", "recording", "mac",
                                  "January backups were moved to the vault.")
        clip = str(uuid4())
        self.clip(clip, kind="voice", started="2026-02-01T07:30:00.000+00:00", zone="America/Vancouver")
        self.assistant.generate_conversation = model([{"name":"archive","query":"backups","date":"last month"}])
        finished = self.transcribe(clip, "Where did we move the backups last month?")
        self.assertEqual(finished["reply"]["engine"], "private_request")
        self.assertEqual(finished["reply"]["sources"], [])
        with self.store.privacy.transaction() as db:
            saved = json.loads(db.execute("SELECT payload FROM requests WHERE id=?", (clip,)).fetchone()[0])
        self.assertEqual(saved["zone"], "America/Vancouver")
        self.assertEqual(saved["asked_at"], "2026-02-01T07:30:00.000+00:00")
        approved = self.assistant.answer(saved["text"], saved["language"], zone=saved["zone"],
                                         asked_at=datetime.fromisoformat(saved["asked_at"]), exclude=clip)
        self.assertEqual(approved["sources"], [december])

    def test_existing_archive_adds_time_zone_without_changing_retry_identity(self):
        clip, started = str(uuid4()), now()
        self.clip(clip, started=started)
        with self.store.connect() as db:
            db.execute("ALTER TABLE recordings DROP COLUMN time_zone")
        self.store = Store(self.root, 0, 1_048_576)
        _, changed = self.clip(clip, started=started)
        self.assertFalse(changed)
        self.assertEqual(self.store.audio_path(clip).read_bytes(), b"test audio")

    def test_correction_reindexes_and_preserves_original_without_repeating_a_reply(self):
        identifier = str(uuid4())
        self.clip(identifier, wake=True)
        original = "The recovery passphrase is pinecone."
        finished = self.transcribe(identifier, original)
        self.assistant.generate_conversation = model(lambda p: [{"name":"archive","query":p["question"].strip("?")}])
        old = self.assistant.answer("Pinecone?", "en")
        self.assertIn(identifier, old["sources"])
        corrected = "Hey Nex, is the recovery passphrase acorn?"
        request_id = str(uuid4())
        saved = self.store.correct_transcript(identifier, "iphone", request_id, 0, corrected, corrected)
        self.assertEqual(saved["origin"], "manual")
        self.assertEqual(saved["history"][0]["previous_text"], original)
        self.assertEqual(saved["history"][0]["previous_origin"], "apple_on_device")
        self.assertEqual(self.store.audio_path(identifier).read_bytes(), b"test audio")
        self.assertEqual(self.store.recording(identifier)["updated"], finished["updated"])
        self.assertIsNone(self.worker.claim())
        self.assertIsNone(self.store.recording(identifier)["reply"])
        self.assertEqual(self.store.source("n-" + identifier)["text"], corrected)
        self.assertIn(identifier, self.assistant.answer("Acorn?", "en")["sources"])
        self.assertEqual(self.assistant.answer("Pinecone?", "en")["sources"], [])
        self.assertEqual(self.store.correct_transcript(
            identifier, "iphone", request_id, 0, corrected, corrected,
        ), saved)
        with self.assertRaises(TranscriptConflict):
            self.store.correct_transcript(identifier, "console", str(uuid4()), 0, original, original)
        with self.assertRaises(TranscriptConflict):
            self.store.correct_transcript(identifier, "iphone", request_id, saved["revision"], original, original)

    def test_pending_claim_recovers_and_model_settings_validate(self):
        identifier = str(uuid4())
        self.clip(identifier)
        self.worker.claim()
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state='queued' WHERE state='processing'")
        self.assertEqual(self.worker.claim()["id"], identifier)
        with self.assertRaises(ValueError):
            self.settings.update({"llama_server": "https://outside.example/runtime"})
        with self.assertRaises(ValueError):
            self.settings.update({"language_model": "../memory.sqlite"})
        for name in ("llama", "sample.gguf"):
            path = self.settings.assets / name
            path.touch()
            os.chmod(path, 0o700)
        self.settings.update({"llama_server": "llama", "language_model": "sample.gguf"})
        persisted = json.loads(self.settings.path.read_text())
        self.assertEqual(persisted["language_model"], "sample.gguf")
        self.assertEqual(self.settings.status()["chat"], "configured")

    def test_aac_decoder_can_be_configured_before_whisper(self):
        for name in ("decoder", "whisper", "multilingual.bin"):
            path = self.settings.assets / name
            path.touch()
            path.chmod(0o700)
        with self.assertRaises(ValueError):
            self.settings.update({"whisper_cli": "whisper", "whisper_model": "multilingual.bin"})
        self.settings.update({"ffmpeg": "decoder"})
        self.assertEqual(self.settings.status()["decoder"], "configured")
        self.assertEqual(self.settings.status()["transcription"], "awaiting_models")
        self.settings.update({"whisper_cli": "whisper", "whisper_model": "multilingual.bin"})
        self.assertEqual(self.settings.status()["transcription"], "configured")
        self.settings.update({"whisper_cli": "", "whisper_model": ""})
        self.assertEqual(self.settings.read()["ffmpeg"], "decoder")

    def test_optional_mac_model_falls_back_to_archive_and_prefers_excalibur(self):
        with self.assertRaises(ValueError):
            self.settings.update({"use_mac_model": "true"})
        self.settings.update({"use_mac_model": True})
        self.assertEqual(self.engines.model_status()["chat"], "mac_waiting")
        self.assertEqual(self.assistant.answer("What can you tell me?", "en")["engine"], "model_unavailable")
        fake = self.root / "runtime" / "mac-model.sock"
        fake.touch()
        self.assertIsNone(self.engines.mac_socket())
        reply = {"choices": [{"message": {"content": "Generated offline"}}]}
        with (
            patch.object(self.engines, "mac_socket", return_value=fake),
            patch.object(self.engines, "request", return_value=reply) as request,
        ):
            answer = self.engines.generate([], 32, self.settings.read())
            self.assertEqual(answer, "Generated offline")
            self.assertEqual(request.call_args.kwargs["socket_path"], fake)
        config = self.settings.read() | {"llama_server": "already-configured"}
        with (
            patch.object(self.engines, "ensure_model") as ensure,
            patch.object(self.engines, "request", return_value=reply) as request,
        ):
            self.assertEqual(self.engines.generate([], 32, config), "Generated offline")
            ensure.assert_called_once_with(config)
            self.assertNotIn("socket_path", request.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
