"""Local text preparation and synthesis for the English live voice."""

import re
from threading import Lock

import sherpa_onnx
from num2words import num2words
from piper.phonemize_espeak import EspeakPhonemizer

DIGITS = re.compile(r"(?<!\w)-?\d+(?:\.\d+)?(?!\w)")


def spoken_numbers(text):
    def convert(match):
        value = match[0]
        if len(value) > 12:
            return " ".join(num2words(int(digit)) for digit in value if digit.isdecimal())
        return num2words(value)
    return DIGITS.sub(convert, text)


class EnglishVoice:
    def __init__(self, assets):
        self.phonemizer = EspeakPhonemizer()
        self.lock = Lock()
        self.engine = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(assets / "model.onnx"), voices=str(assets / "voices.bin"),
                    tokens=str(assets / "tokens.txt"),
                ), num_threads=2,
            ),
        ))

    def generate(self, text):
        text = spoken_numbers(text.replace("’", "'"))
        # eSpeak uses process-wide voice state. A cancelled turn can still be finishing on a worker.
        with self.lock:
            sentences = self.phonemizer.phonemize("en-us", text)
            if not sentences:
                return ()
            config = sherpa_onnx.GenerationConfig()
            config.phoneme_codepoints = [[ord(char) for char in sentence] for sentence in sentences]
            return self.engine.generate(text, config).samples
