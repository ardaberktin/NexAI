"""Reserve the shared live slot while a recorder switches from standby."""

import asyncio
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

TICKET_LIFETIME = 8
WAKE_RESERVATION_LIMIT = 30  # Covers the 22-second maximum utterance and final recognition.


@dataclass
class AttentionTicket:
    device: str
    token: str
    text: str = ""
    named: bool = False
    audio: bytes = b""


class OpeningTickets:
    def __init__(self):
        self.pending = None
        self.resolved = None
        self.expiry = None
        self.deadline = 0

    def _reserve(self, device):
        self.pending = AttentionTicket(device, str(uuid4()))
        self.resolved = asyncio.get_running_loop().create_future()
        return self.pending.token

    def reserve_wake(self, device, live_active):
        if live_active.locked() or self.pending:
            return None
        token = self._reserve(device)
        self.expire_after(WAKE_RESERVATION_LIMIT)
        return token

    def waking(self, device, token):
        return self.matches(device, token) and not self.pending.text

    def expire_after(self, seconds):
        if self.expiry:
            self.expiry.cancel()
        self.deadline = monotonic() + seconds
        self.expiry = asyncio.get_running_loop().call_later(seconds, self._finish, False)

    async def offer(self, device, text, named, audio, live_active, wake_token):
        # Two microphones can hear the same question. A declined offer (e.g. local
        # media playing) lets the other recorder take it. A claimed offer ends the race.
        if live_active.locked():
            return None
        if not self.waking(device, wake_token):
            if self.pending and not await self.wait_for_decline(device):
                return None
            if live_active.locked() or self.pending:
                return None
            self._reserve(device)
        self.pending.text, self.pending.named, self.pending.audio = text, named, audio
        self.expire_after(TICKET_LIFETIME)
        return self.pending.token

    async def wait_for_decline(self, device):
        if self.pending.device == device:
            return False
        try:
            async with asyncio.timeout(TICKET_LIFETIME):
                return not await asyncio.shield(self.resolved)
        except TimeoutError:
            return False

    def matches(self, device, token):
        return self.pending is not None and self.pending.device == device and self.pending.token == token

    def claim(self, device, token):
        if not self.matches(device, token) or not self.pending.text:
            return None
        ticket = self.pending
        valid = monotonic() < self.deadline
        self._finish(valid)
        return ticket if valid else None

    def decline(self, device, token):
        if self.matches(device, token):
            self._finish(False)

    def _finish(self, claimed):
        if self.expiry:
            self.expiry.cancel()
        self.resolved.set_result(claimed)
        self.pending = self.resolved = self.expiry = None
