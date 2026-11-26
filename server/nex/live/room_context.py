"""Identify audible media and other speakers in a short, in-memory room sample."""

import logging
from collections import defaultdict
from threading import Lock

import numpy as np
import sherpa_onnx

from . import INPUT_RATE, PLAYBACK_LABELS, VOICE_LABELS

LOG = logging.getLogger(__name__)
ASSETS = ("model.int8.onnx", "class_labels_indices.csv", "segmentation.onnx", "speaker.onnx")
MUSIC_LABELS = {"Music", "Musical instrument", "Singing"}


class RoomContext:
    @classmethod
    def load(cls, assets):
        directory = assets / "room-context"
        if not all((directory / name).is_file() for name in ASSETS):
            LOG.warning("Room audio models are missing. Automatic voice is using speech context only.")
            return None
        context = cls(directory)
        LOG.info("Room audio models are ready")
        return context

    def __init__(self, directory):
        self.lock = Lock()
        tagger = sherpa_onnx.AudioTaggingConfig(
            model=sherpa_onnx.AudioTaggingModelConfig(ced=str(directory / "model.int8.onnx"), num_threads=1),
            labels=str(directory / "class_labels_indices.csv"),
            top_k=20,
        )
        embedding = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(directory / "speaker.onnx"))
        speakers = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(directory / "segmentation.onnx"), window_shift_ratio=0.25,
                ),
            ),
            embedding=embedding,
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5),
            min_duration_on=0.35,
            min_duration_off=0.4,
        )
        if not tagger.validate() or not speakers.validate():
            raise ValueError("The room audio models could not be configured.")
        self.tagger = sherpa_onnx.AudioTagging(tagger)
        self.speakers = sherpa_onnx.OfflineSpeakerDiarization(speakers)
        self.embedding = sherpa_onnx.SpeakerEmbeddingExtractor(embedding)

    def busy(self, audio, context=b""):
        samples = np.frombuffer(context + audio, dtype="<i2").astype(np.float32) / 32768
        offset = len(context) // 2
        with self.lock:
            if self.has_media(samples[:offset]) or self.has_media(samples[offset:]):
                return True
            segments = self.speakers.process(samples).sort_by_start_time()
            return self.has_other_speech(samples, offset, segments)

    def has_other_speech(self, samples, offset, segments):
        boundary = offset / INPUT_RATE
        durations = defaultdict(float)
        prior_speech, question_speech = 0.0, 0.0
        for segment in segments:
            durations[segment.speaker] += segment.end - segment.start
            prior_speech += max(0, min(segment.end, boundary) - segment.start)
            question_speech += max(0, segment.end - max(segment.start, boundary))
        if sum(duration >= 0.6 for duration in durations.values()) >= 2:
            return True
        if min(prior_speech, question_speech) < 0.65:
            return False
        # The diarizer can merge two short turns. Compare the voices across the
        # known question boundary as a second check.
        before, question = self.voice(samples[:offset]), self.voice(samples[offset:])
        return before is not None and question is not None and float(before @ question) < 0.55

    def voice(self, samples):
        audible = np.flatnonzero(np.abs(samples) >= 0.01)
        if not len(audible) or audible[-1] - audible[0] < INPUT_RATE * 0.65:
            return None
        stream = self.embedding.create_stream()
        stream.accept_waveform(INPUT_RATE, samples[audible[0]:audible[-1] + 1])
        stream.input_finished()
        if not self.embedding.is_ready(stream):
            return None
        vector = np.asarray(self.embedding.compute(stream))
        norm = np.linalg.norm(vector)
        return vector / norm if norm > 0 else None

    def has_media(self, samples):
        return any(
            (event.name in PLAYBACK_LABELS and event.prob >= 0.2)
            or ((event.name in MUSIC_LABELS or event.name.endswith(" music")) and event.prob >= 0.35)
            for event in self.events(samples)
        )

    def events(self, samples):
        if len(samples) < INPUT_RATE // 2 or np.max(np.abs(samples)) < 0.008:
            return ()
        stream = self.tagger.create_stream()
        stream.accept_waveform(sample_rate=INPUT_RATE, waveform=samples)
        return self.tagger.compute(stream)

    def sound_labels(self, pcm):
        labels, _, _, _ = self.clip_audio(pcm, diarize=False)
        return labels

    def clip_audio(self, pcm, *, diarize=True):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        scores = {}
        window = 10 * INPUT_RATE
        for offset in range(0, len(samples), window):
            with self.lock:
                events = self.events(samples[offset:offset + window])
            for event in events:
                if event.prob >= 0.2:
                    scores[event.name] = max(scores.get(event.name, 0), event.prob)
        labels = sorted(scores, key=scores.get, reverse=True)[:8]
        if not diarize or not VOICE_LABELS.intersection(labels) or len(samples) < INPUT_RATE:
            return labels, None, samples, ()
        with self.lock:
            segments = list(self.speakers.process(samples).sort_by_start_time())
        durations = defaultdict(float)
        for segment in segments:
            durations[segment.speaker] += max(0.0, segment.end - segment.start)
        active = {speaker for speaker, duration in durations.items() if duration >= 0.5}
        if not active:
            active = {speaker for speaker, duration in durations.items() if duration >= 0.35}
        turns, previous = 0, None
        for segment in segments:
            if segment.speaker not in active or segment.end - segment.start < 0.35:
                continue
            if previous is not None and segment.speaker != previous:
                turns += 1
            previous = segment.speaker
        total_speech = sum(durations[speaker] for speaker in active)
        clip_seconds = max(len(samples) / INPUT_RATE, 0.001)
        profile = {
            "speaker_count": len(active),
            "speaker_turns": turns,
            "speech_ratio": round(min(1.0, total_speech / clip_seconds), 2),
        }
        return labels, profile, samples, segments
