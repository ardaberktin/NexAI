"""Check unclear streaming hypotheses against the existing local Whisper model."""

import asyncio
import re
import unicodedata
import wave
from difflib import SequenceMatcher
from pathlib import Path
from tempfile import TemporaryDirectory

from ..conversation_intent import CLIPPED_GREETING, words
from ..lookup_topics import current_topic
from ..transcription import has_spoken_text, read_whisper_transcript
from . import INPUT_RATE

INCOMPLETE = re.compile(r"^(?:i|we|the|a|an|ben|biz|bir|what|who|when|where|why|how|how are|ne|nasil)$|"
                        r"\b(?:and|or|with|about|the|how|where|why|which|ve|veya|ile|nasil|hangi)$")


def unfinished(text):
    clean = words(text)
    return bool(CLIPPED_GREETING.fullmatch(clean) or INCOMPLETE.search(clean))


def unclear(text):
    letters = [character for character in text if character.isalpha()]
    return (not has_spoken_text(text) or unfinished(text)
            or any(not unicodedata.name(char, "").startswith("LATIN") for char in letters))


async def verify_public_name(verifier, audio, language, text):
    # Acronyms and numbered titles often sound plausible in the first pass even when one letter is wrong.
    if current_topic(text) and re.search(r"\b[A-Z]{2,}\b", text):
        alternative = await verifier.recover(audio, language)
        if current_topic(alternative) and SequenceMatcher(None, words(text), words(alternative)).ratio() >= 0.65:
            return alternative
    return text


class Verifier:
    def __init__(self, assets, scratch):
        self.executable = assets / "whisper-cpp-1.9.5-fast" / "bin" / "whisper-cli"
        self.model = assets / "whisper-cpp-1.9.5" / "models" / "ggml-small-q5_1.bin"
        self.scratch = scratch

    async def check(self, audio, language, transcript):
        if not unclear(transcript):
            return await verify_public_name(self, audio, language, transcript)
        alternative = await self.recover(audio, language)
        if alternative and not unclear(alternative):
            return alternative
        # Preserve just the greeting when both passes leave the question incomplete.
        greeting = CLIPPED_GREETING.fullmatch(words(transcript))
        return greeting[1].capitalize() if greeting else ""

    async def recover(self, audio, language, *, wake=False, weather=False):
        if not self.executable.is_file() or not self.model.is_file():
            return ""
        with TemporaryDirectory(prefix="asr-", dir=self.scratch) as temporary:
            prefix = Path(temporary) / "turn"
            source = prefix.with_suffix(".wav")
            with wave.open(str(source), "wb") as file:
                file.setparams((1, 2, INPUT_RATE, 0, "NONE", "not compressed"))
                file.writeframes(audio)
            context = max(512, 64 + len(audio) * 50 // (2 * INPUT_RATE))
            prompt = []
            if weather:
                prompt = ["--prompt", "Nex. What is the weather like outside today?"]
            elif wake:
                prompt = ["--prompt", "Nex. Hey Nex. Merhaba Nex. Selam Nex."]
            process = await asyncio.create_subprocess_exec(
                str(self.executable), "-m", str(self.model), "-f", str(source), "-l", language,
                "-ac", str(context), "-t", "3", "-ng", "-nt", "-bs", "3", "-bo", "1",
                "-nf", "-sns", "-ojf", "-of", str(prefix), *prompt,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), 7)
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if asyncio.current_task().cancelling():
                    raise
                return ""
            if process.returncode != 0:
                return ""
            try:
                text = read_whisper_transcript(prefix.with_suffix(".json"))
            except (OSError, ValueError, KeyError, TypeError):
                return ""
        return text
