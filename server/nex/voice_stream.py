import re
import threading
import time
from collections import OrderedDict

# Leave decimals such as 3.14 intact and prefer natural sentence or clause breaks.
SENTENCE_END = re.compile(r"[!?…](?:[\"”’)]*)?(?=\s|$)|\.(?:[\"”’)]*)?(?=\s)")
CLAUSE_END = re.compile(r"[,;:](?=\s)")
PENDING = frozenset({"queued", "processing"})


class SpeechChunks:
    def __init__(self, emit):
        self.emit = emit
        self.buffer = ""

    def add(self, text, language):
        self.buffer += text
        self.flush(language)

    def flush(self, language, *, final=False):
        while self.buffer:
            boundary = self.find_boundary()
            if boundary is None and not final:
                return
            end = boundary or len(self.buffer)
            spoken, self.buffer = self.buffer[:end], self.buffer[end:]
            if spoken.strip():
                self.emit(spoken, language)

    def find_boundary(self):
        sentence = SENTENCE_END.search(self.buffer)
        if sentence:
            return sentence.end()
        clause = next((match for match in CLAUSE_END.finditer(self.buffer) if match.end() >= 26), None)
        if clause:
            return clause.end()
        if len(self.buffer) > 105:
            space = self.buffer.rfind(" ", 55, 105)
            if space >= 0:
                return space + 1
        return None


class VoiceStreams:
    """Retain recent chunks so a client can subscribe after uploading its clip."""

    def __init__(self, recording):
        self.recording = recording
        self.changed = threading.Condition()
        self.turns = OrderedDict()

    def start(self, identifier):
        self._publish(identifier, {"kind": "chime"}, restart=True)

    def publish(self, identifier, text, language):
        self._publish(identifier, {"kind": "speech", "text": text, "language": language})

    def _publish(self, identifier, event, *, restart=False):
        with self.changed:
            chunks = [] if restart else self.turns.get(identifier, (0, []))[1]
            chunks.append(event)
            self.turns[identifier] = (time.monotonic(), chunks)
            self.turns.move_to_end(identifier)
            self.prune()
            self.changed.notify_all()

    def prune(self):
        limit = time.monotonic() - 180
        while self.turns:
            oldest, (stamp, _) = next(iter(self.turns.items()))
            if stamp >= limit and len(self.turns) <= 64:
                break
            self.turns.pop(oldest)

    def events(self, identifier, *, timeout=120):
        end = time.monotonic() + timeout
        heartbeat = time.monotonic() + 5
        cursor = 0
        while time.monotonic() < end:
            with self.changed:
                self.prune()
                chunks = self.turns.get(identifier, (0, ()))[1][cursor:]
                if not chunks:
                    row = self.recording(identifier)
                    if not row or row["state"] not in PENDING:
                        final = {"kind": "complete", "reply": row["reply"] if row else None,
                                 "state": row["state"] if row else None}
                        break
                    delay = min(0.5, end - time.monotonic())
                    self.changed.wait(max(0, delay))
            if chunks:
                cursor += len(chunks)
                yield from chunks
            if time.monotonic() >= heartbeat:
                yield {"kind": "waiting"}
                heartbeat = time.monotonic() + 5
        else:
            final = {"kind": "timeout"}
        yield final
