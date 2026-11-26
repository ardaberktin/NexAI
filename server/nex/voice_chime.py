"""The same pickup sound is bundled with both Apple apps and the voice services."""

import io
import wave
from pathlib import Path

CHIME_WAV = (Path(__file__).with_name("sounds") / "question-chime.wav").read_bytes()
with wave.open(io.BytesIO(CHIME_WAV)) as chime:
    CHIME_PCM = chime.readframes(chime.getnframes())
