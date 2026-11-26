import re
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row

from .store import normalized

CALL = r"(?:(?:hey|merhaba|selam)[\s,]+(?:nex|neks|next)\b[\s,:.-]*|(?:nex|neks)\s*,\s*)"
ADDRESS = re.compile(r"(?:^|(?<=[.!?])\s+)" + CALL, re.IGNORECASE)
LEADING_WAKE = re.compile(r"^(?:(?:hey|merhaba|selam)[\s,]+)?(?:nex|neks|necks)\b[\s,.:!?-]*", re.IGNORECASE)
SPOKEN_WAKE_VARIANT = re.compile(
    r"^(?:(?:hey|merhaba|selam)[\s,]+next|hay[\s,]+(?:nex|next)|hey[\s,]+nax|hanx)\b[\s,.:!?-]*", re.IGNORECASE
)
BARE_NEXT = re.compile(r"^next(?=$|[,\s:!?]|[.-](?:\s|$))[\s,.:!?-]*", re.IGNORECASE)
TRAILING_WAKE = re.compile(r"[\s,.:!?]+(?:nex|neks|necks|nax)\b[\s,.:!?-]*$", re.IGNORECASE)
TRANSCRIBED_NAME = re.compile(r"[,.:!?]\s*(?:nyx|nix)\b[\s.!?]*$", re.IGNORECASE)
TRAILING_NEXT = re.compile(r"[,.:!?]\s*next[\s.!?]*$", re.IGNORECASE)
GREETING = re.compile(r"(?:^|(?<=[.!?])\s+)(hey|merhaba|selam)[\s,.!-]*$", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
ENGLISH_START = re.compile(
    r"^(what(?:['’]?s)?|who|where|when|why|how(?:['’]?s)?|which|can|could|would|were|was|is|are|did|do|does|have|has|will|should|"
    r"calculate|compute|(?:use|run) python|write (?:a |some )?python)\b"
)
TURKISH_QUESTION = re.compile(
    r"\b(ne(?:dir|ydi)?|neden|nasil|nerede|nereden|kim|hangi|kac(?:tir|ti)?|mi|mu|misin|musun|miyim|muyum)\b"
)
INCOMPLETE = {
    "a", "an", "and", "are", "at", "did", "do", "does", "for", "from", "had", "has", "have", "in", "is",
    "of", "on", "or", "that", "the", "to", "was", "were", "will", "with", "bir", "bu", "da", "hangi",
    "kac", "ve", "veya",
}


def spoken_wake_body(text):
    match = LEADING_WAKE.match(text) or SPOKEN_WAKE_VARIANT.match(text)
    if match:
        return text[match.end():].strip()
    return before_wake(text, TRAILING_WAKE) or before_wake(text, TRAILING_NEXT) or before_wake(text, TRANSCRIBED_NAME)


def next_wake_body(text):
    match = BARE_NEXT.match(text)
    return text[match.end():].strip() if match else None


def before_wake(text, pattern):
    if match := pattern.search(text):
        question = text[:match.start()].strip()
        if question:
            return question + ("?" if "?" in match[0] else "")
    return None


def question_body(text):
    first = SENTENCE.split(text.strip(), maxsplit=1)[0].strip()
    if first.endswith("?") and re.search(r"\w", first):
        return first
    words = normalized(first).strip(" ,.!:;-").split()
    if len(words) < 2 or words[-1] in INCOMPLETE:
        return None
    cleaned = " ".join(words)
    if (len(words) >= 3 and ENGLISH_START.search(cleaned)) or TURKISH_QUESTION.search(cleaned):
        return first
    return None


def addressed_question(text):
    for match in ADDRESS.finditer(text):
        if question := question_body(text[match.end():]):
            return question
    trailing = before_wake(text, TRAILING_WAKE) or before_wake(text, TRAILING_NEXT)
    return question_body(trailing) if trailing else None


def continued_question(previous, current):
    if addressed_question(previous):
        return None
    matches = list(ADDRESS.finditer(previous))
    if matches:
        fragment = previous[matches[-1].end():].strip()
        if len(SENTENCE.split(fragment)) == 1 and len(fragment.split()) <= 12 and "?" not in fragment:
            head = fragment.rstrip(" .,!:-")
            clean = normalized(head)
            if not clean or ENGLISH_START.search(clean) or TURKISH_QUESTION.search(clean):
                return question_body((head + " " + current.lstrip()).strip())
    if greeting := GREETING.search(previous):
        # A clip can stop between the greeting and the name.
        return addressed_question(greeting[1] + " " + current.lstrip())
    return None


def recently_listening(row, at):
    age = (at - datetime.fromisoformat(row["started"])).total_seconds()
    return row["mode"] == "ambient" and row["wake"] and 0 <= age <= 85


def contiguous(earlier, later):
    distance = (datetime.fromisoformat(later["started"]) -
                datetime.fromisoformat(earlier["started"])).total_seconds()
    return earlier["language"] == later["language"] and abs(distance - earlier["duration"]) <= 0.22


@dataclass(frozen=True)
class SpokenQuestion:
    text: str
    target: Row
    neighbor: Row | None = None


def find_question(store, row, text, at=None):
    if not text.strip():
        return None
    if row["mode"] == "voice":
        if row["wake"] and (question := spoken_wake_body(text)) is not None:
            text = question
        return SpokenQuestion(text, row) if text else None
    at = at or datetime.now(UTC)
    if not recently_listening(row, at):
        return None
    if direct := addressed_question(text):
        return SpokenQuestion(direct, row)
    for before in (True, False):
        peer = store.neighbor(row["device"], row["started"], before)
        if (peer is None or peer["state"] != "ready" or peer["reply"] is not None
                or peer["origin"] == "manual" or not peer["transcript"] or not recently_listening(peer, at)):
            continue
        first, second = (peer, row) if before else (row, peer)
        if not contiguous(first, second):
            continue
        head, tail = (peer["transcript"], text) if before else (text, peer["transcript"])
        if combined := continued_question(head, tail):
            return SpokenQuestion(combined, second, peer)
    return None
