"""Local owner voice templates. A match is a speaker label, never an access credential."""

import base64
import hashlib
import hmac
import json
from itertools import combinations

import numpy as np
from cryptography.fernet import Fernet, InvalidToken

from . import INPUT_RATE

SAMPLE_BYTES = 4 * INPUT_RATE * 2
ENROLLMENT_BYTES = 3 * SAMPLE_BYTES
MATCH_THRESHOLD = 0.72


class VoiceProfiles:
    def __init__(self, state, room, server_secret):
        self.state, self.room = state, room
        key = hmac.digest(server_secret.encode(), b"Nex voice templates v1", "sha256")
        self.cipher = Fernet(base64.urlsafe_b64encode(key))

    def vector(self, raw):
        if not self.room or len(raw) < 2 * INPUT_RATE or len(raw) % 2:
            return None
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        with self.room.lock:
            if self.room.has_media(samples):
                return None
            durations = {}
            for segment in self.room.speakers.process(samples).sort_by_start_time():
                durations[segment.speaker] = durations.get(segment.speaker, 0) + segment.end - segment.start
            if sum(n >= 0.4 for n in durations.values()) != 1 or max(durations.values(), default=0) < 1.0:
                return None
            return self.room.voice(samples)

    def enrollment(self, raw):
        if len(raw) != ENROLLMENT_BYTES:
            raise ValueError("Record three four-second voice samples.")
        samples = [self.vector(raw[pos:pos + SAMPLE_BYTES]) for pos in range(0, len(raw), SAMPLE_BYTES)]
        if any(v is None for v in samples):
            raise ValueError("Record each phrase clearly, with one speaker and no background media.")
        if any(float(a @ b) < 0.55 for a, b in combinations(samples, 2)):
            raise ValueError("The samples did not sound consistent. Record the three phrases again.")
        mean = np.mean(samples, axis=0)
        return self.cipher.encrypt(json.dumps((mean / np.linalg.norm(mean)).tolist()).encode())

    def identify(self, raw):
        with self.state.transaction() as db:
            rows = db.execute("SELECT template FROM voices").fetchall()
        if not rows or self.room is None:
            return "unverified"
        try:
            vector = self.vector(raw)
        except (ValueError, RuntimeError):
            return "unverified"
        if vector is not None:
            for row in rows:
                try:
                    known = np.array(json.loads(self.cipher.decrypt(row[0])))
                    if known.shape == vector.shape and float(known @ vector) >= MATCH_THRESHOLD:
                        return "recognized"
                except (InvalidToken, ValueError, TypeError):
                    continue
        return "unverified"


def enrollment_subject(raw):
    return "enroll-" + hashlib.sha256(raw).hexdigest()
