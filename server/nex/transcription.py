"""Reject unsupported or low-confidence automatic speech hypotheses."""

import json
import math
import unicodedata

UNRECOGNIZED = "Nex couldn’t transcribe this recording reliably."
MINIMUM_LOG_PROBABILITY = math.log(0.45)


def has_spoken_text(text):
    return any(char.isdecimal() or (char.isalpha() and unicodedata.name(char, "").startswith("LATIN"))
               for char in text)


def read_whisper_transcript(path):
    # whisper.cpp's detailed JSON can contain a fragment of a UTF-8 character inside a token.
    # The assembled segment can still contain perfectly valid text.
    return whisper_text(json.loads(path.read_text(encoding="utf-8", errors="replace")))


def whisper_text(output):
    accepted = []
    for segment in output["transcription"]:
        text = segment["text"].strip()
        if "\ufffd" in text or not has_spoken_text(text):
            continue
        probabilities = [token["p"] for token in segment["tokens"]
                         if "\ufffd" not in token["text"] and not token["text"].startswith("[_")
                         and has_spoken_text(token["text"])]
        # The geometric mean keeps common high-confidence words from masking a badly decoded phrase.
        if probabilities and sum(math.log(max(p, 1e-12)) for p in probabilities) >= len(probabilities) * MINIMUM_LOG_PROBABILITY:
            accepted.append(text)
    return " ".join(accepted)


def has_audible_input(samples, *, spoken):
    if not spoken:
        return any(abs(value) >= 140 for value in samples)
    # A click can exceed the peak threshold even when the entire question is room noise.
    # Require at least two 50 ms windows above a conservative floor at 16 kHz.
    window = 800
    audible = 0
    for offset in range(0, len(samples) - window + 1, window):
        if sum(sample * sample for sample in samples[offset:offset + window]) >= window * 100 ** 2:
            audible += 1
            if audible == 2:
                return True
    return False
