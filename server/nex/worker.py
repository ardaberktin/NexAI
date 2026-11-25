import json
import logging
import threading

from .conversation_history import watch_dialogue
from .live_memory import attach_reply, read_result
from .models import ModelError
from .personal_learning import enqueue_context
from .privacy.public import answer as public_answer
from .privacy.recorded import recorded_speaker
from .recorded_intent import select_request
from .store import now
from .transcription import UNRECOGNIZED, has_spoken_text
from .voice_stream import VoiceStreams
from .wake import find_question

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, engines, assistant, settings, *, enabled=lambda: True):
        self.store, self.engines, self.assistant, self.settings = store, engines, assistant, settings
        self.enabled = enabled
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.voice_streams = VoiceStreams(store.recording)

    def start(self):
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state='queued' WHERE state='processing'")
        for voice in (True, False):
            threading.Thread(target=self.run, args=(voice,), name="voice-worker" if voice else "recording-worker",
                             daemon=True).start()
        threading.Thread(target=self.engines.piper.warm, args=(self.settings.read(),),
                         name="piper-warmup", daemon=True).start()

    def configure(self):
        enabled = self.settings.status()["transcription"] == "configured"
        if enabled:
            with self.store.connect() as db:
                db.execute("UPDATE recordings SET state='queued',error=NULL WHERE state='awaiting_transcription'")
        if not self.settings.read()["llama_server"]:
            threading.Thread(target=self.release_chat, daemon=True).start()
        self.wake.set()

    def release_chat(self):
        with self.engines.lock:
            if not self.settings.read()["llama_server"]:
                self.engines.stop()

    def local_transcript(self, identifier, device, value):
        with self.store.connect() as db:
            cursor = db.execute(
                """UPDATE recordings SET transcript=?,origin='apple_on_device',state='queued',error=NULL,updated=?
                   WHERE id=? AND device=? AND transcript IS NULL
                   AND state IN ('queued','awaiting_transcription','failed')""",
                (value, now(), identifier, device),
            )
        self.wake.set()
        return cursor.rowcount > 0

    def retry(self, identifier):
        with self.store.connect() as db:
            cursor = db.execute(
                """UPDATE recordings SET state='queued', error=NULL, updated=?
                   WHERE id=? AND state IN ('failed','awaiting_transcription')""",
                (now(), identifier),
            )
        self.wake.set()
        return cursor.rowcount > 0

    def claim(self, voice=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM recordings WHERE state='queued' AND (? IS NULL OR (mode='voice')=?)
                   ORDER BY CASE mode WHEN 'voice' THEN 0 ELSE 1 END,
                       CASE WHEN wake THEN 0 ELSE 1 END,
                       CASE WHEN wake THEN started END DESC, started LIMIT 1""",
                (voice, voice),
            ).fetchone()
            if row:
                db.execute("UPDATE recordings SET state='processing', updated=? WHERE id=?", (now(), row["id"]))
            return row

    def process(self, row):
        config = self.settings.read()
        identifier = row["id"]
        transcript, origin = row["transcript"], row["origin"]
        handled = row["live_handled"]
        if handled and transcript is None:
            with self.store.connect() as db:
                live = read_result(db, row)
            if live and live["transcript"] is not None:
                transcript, origin = live["transcript"], "live_asr"
        transcription_ready = all(self.settings.resolve(config[key])
                                  for key in ("whisper_cli", "whisper_model", "ffmpeg"))
        if transcript is None and not transcription_ready:
            with self.store.connect() as db:
                db.execute("UPDATE recordings SET state='awaiting_transcription',updated=? WHERE id=?",
                           (now(), identifier))
            return
        if transcript is None:
            transcript = self.engines.transcribe(
                self.store.audio_path(identifier), row["language"], config, wake=bool(row["wake"]),
                spoken=row["mode"] == "voice" or bool(row["wake"]),
            )
            origin = "whisper"
        transcript = transcript[:12_000] if has_spoken_text(transcript) else ""
        empty_state = "unrecognized" if row["mode"] == "voice" else "quiet"
        state = "ready" if transcript else empty_state
        def review(candidates):
            def generate(messages, tokens):
                return self.assistant.generate_conversation(messages, config, row["language"], tokens, None,
                                                            use_warm=True, temperature=0)
            return select_request(candidates, generate)

        question = None if handled else find_question(self.store, row, transcript, select=review)
        exclude = question.neighbor["id"] if question and question.neighbor else identifier
        reply = self.answer_spoken(question, row["language"], exclude) if question else None
        if handled or question or not transcript:
            notes, engine = "", None
        else:
            notes, engine = self.assistant.process_notes(transcript, config)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if transcript:
                self.store.add_memory(db, identifier, row["started"], "recording", row["device"], transcript)
                if not question and row["mode"] == "ambient":
                    enqueue_context(db, "ambient-" + identifier, "ambient", transcript, row["started"])
                if notes:
                    self.store.add_memory(db, "n-" + identifier, row["started"], "note", row["device"], notes,
                                          [identifier], engine)
            if reply and reply["engine"] != "account_read":
                self.store_reply(db, row, question, reply)
            db.execute(
                """UPDATE recordings SET transcript=?,origin=?,notes=?,state=?,error=?,updated=?
                   WHERE id=?""",
                (transcript, origin, notes, state, UNRECOGNIZED if state == "unrecognized" else None,
                 now(), identifier),
            )

            if handled:
                live = read_result(db, row)
                if live and live["reply"]:
                    attach_reply(self.store, db, row, json.loads(live["reply"]))

    def answer_spoken(self, question, preference, exclude):
        target = question.target
        identifier = target["id"]
        self.voice_streams.start(identifier)
        streamed = False

        def emit(text, language):
            nonlocal streamed
            streamed = True
            self.voice_streams.publish(identifier, text, language)

        speaker = recorded_speaker(self.store, self.engines, question, self.settings.read())
        history = watch_dialogue(self.store, target) if target["device"] == "watch" and target["watch_conversation"] else ()
        result = public_answer(self.assistant, self.store.privacy, identifier, target["device"],
                               question.text, preference, target["time_zone"], spoken=True,
                               asked_at=target["started"], speaker=speaker, emit=emit, exclude=exclude, history=history)
        if not streamed and result["text"]:
            emit(result["text"], result["language"])
        return result | {"speaker": speaker}

    def store_reply(self, db, row, question, reply):
        target = question.target
        sources = [row["id"]]
        peer = question.neighbor
        if peer:
            # A client can correct the adjacent transcript while the model is generating.
            live = db.execute("SELECT state,transcript,origin,reply FROM recordings WHERE id=?",
                              (peer["id"],)).fetchone()
            if not live or tuple(live) != ("ready", peer["transcript"], peer["origin"], None):
                return
            sources = [peer["id"], row["id"]] if target["id"] == row["id"] else [row["id"], peer["id"]]
        updated = db.execute("UPDATE recordings SET reply=?,updated=? WHERE id=? AND reply IS NULL",
                             (json.dumps(reply), now(), target["id"]))
        if updated.rowcount and reply["text"] and reply["engine"] != "private_request":
            citations = list(dict.fromkeys(sources + reply["sources"]))
            self.store.add_memory(db, "q-" + target["id"], target["started"], "question", target["device"],
                                  question.text)
            self.store.add_memory(db, "a-" + target["id"], target["started"], "answer", target["device"],
                                  reply["text"], citations, reply["engine"], recall_cursor=reply.get("recall_cursor"))

    def run(self, voice):
        while not self.stop.is_set():
            row = self.claim(voice) if self.enabled() else None
            if row is None:
                self.wake.wait(0.25 if voice else 1)
                self.wake.clear()
                continue
            try:
                self.process(row)
            except Exception as exc:  # noqa: BLE001 - isolate a failed recording and keep the worker alive.
                error = str(exc) if isinstance(exc, ModelError) else "The recording could not be processed."
                logger.warning("Recording processing failed (%s)", type(exc).__name__)
                with self.store.connect() as db:
                    db.execute("UPDATE recordings SET state='failed',error=?,updated=? WHERE id=?",
                               (error, now(), row["id"]))
