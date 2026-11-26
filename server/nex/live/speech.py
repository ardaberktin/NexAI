"""CPU speech engines and per-connection streaming recognition."""

import threading

import numpy as np
import sherpa_onnx
from ema_lightning import EMA
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from . import INPUT_RATE, OUTPUT_RATE
from .english_voice import EnglishVoice

LEAD_IN_SAMPLES = INPUT_RATE // 5
START_SECONDS = 0.096


class Speech:
    def __init__(self, assets):
        self.recognition_lock = threading.Lock()
        asr = assets / "nemotron"
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(asr / "tokens.txt"), encoder=str(asr / "encoder.int8.onnx"),
            decoder=str(asr / "decoder.int8.onnx"), joiner=str(asr / "joiner.int8.onnx"),
            num_threads=2, model_type="nemo_transducer",
        )
        self.english = EnglishVoice(assets / "kokoro-en-v0_19")
        import torch
        torch.set_num_threads(2)
        self.turkish = EMA(device="cpu")
        self.turkish_lock = threading.Lock()
        # Exercise both answer voices before serving the first live connection.
        for language in ("en", "tr"):
            if not b"".join(self.speak("Nex.", language)):
                raise RuntimeError(f"The {language} live voice did not generate audio")

    def transcribe_candidate(self, audio, language):
        stream = self.recognizer.create_stream()
        stream.set_option("language", language)
        samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768
        padded = np.pad(samples, (LEAD_IN_SAMPLES, INPUT_RATE * 8 // 10))
        stream.accept_waveform(INPUT_RATE, padded)
        stream.input_finished()
        with self.recognition_lock:
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
            return str(self.recognizer.get_result(stream)).strip()

    def listen(self, language):
        return Listening(self.recognizer, language, self.recognition_lock)

    def turkish_chunks(self, text):
        with self.turkish_lock:
            yield from self.turkish.stream(text, sample_rate=OUTPUT_RATE)

    def speak(self, text, language):
        if language == "tr":
            chunks = self.turkish_chunks(text)
        else:
            chunks = (self.english.generate(text),)
        for chunk in chunks:
            samples = np.asarray(np.clip(np.asarray(chunk), -1, 1) * 32767, dtype="<i2")
            if samples.size:
                yield samples.tobytes()


class Listening:
    def __init__(self, recognizer, language, recognition_lock):
        self.recognizer = recognizer
        self.language = language
        self.recognition_lock = recognition_lock
        self.vad = SileroVADAnalyzer(sample_rate=INPUT_RATE, params=VADParams(
            confidence=0.5, start_secs=START_SECONDS, stop_secs=0.32, min_volume=0.24,
        ))
        self.vad.set_sample_rate(INPUT_RATE)
        self.smart = LocalSmartTurnAnalyzerV3(sample_rate=INPUT_RATE, cpu_count=1)
        self.smart.set_sample_rate(INPUT_RATE)
        self.smart.update_vad_start_secs(START_SECONDS)
        self.stream = None

    async def prime_vad(self):
        # Give Silero baseline context before the first syllable of a new session.
        # An integral number of Silero frames leaves the microphone timeline aligned.
        await self.vad.analyze_audio(bytes(2 * self.vad.num_frames_required() * 6))

    def begin(self):
        self.stream = self.recognizer.create_stream()
        self.stream.set_option("language", self.language)
        # Give the encoder context before the utterance without shifting the archive's audio offsets.
        self.stream.accept_waveform(INPUT_RATE, np.zeros(LEAD_IN_SAMPLES, dtype=np.float32))

    def feed(self, audio):
        samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768
        self.stream.accept_waveform(INPUT_RATE, samples)
        return self.decode()

    def decode(self):
        with self.recognition_lock:
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
            return str(self.recognizer.get_result(self.stream)).strip()

    def finish(self):
        # Flush the final chunk with the right context used by the Sherpa streaming CLI.
        self.feed(bytes(2 * 800 * INPUT_RATE // 1000))
        self.stream.input_finished()
        result = self.decode()
        self.stream = None
        return result
