"""Short, conditional waits for shared chat and recording status."""

from math import isfinite
from threading import Condition
from time import monotonic, sleep, time

from .workspace_chat_pages import paged_chat_snapshot
from .workspace_content import chat_snapshot

MAX_WAIT = 12
CHANGE_INTERVAL = 0.25
RECORDING_LEASE = 30
RECORDERS = ("mac", "iphone", "watch")


def watch_chat(store, cursor, *, paged=False):
    # The live voice service commits from a different process. data_version
    # observes those commits too, without holding a read transaction while idle.
    deadline = monotonic() + MAX_WAIT
    version = None
    read_snapshot = paged_chat_snapshot if paged else chat_snapshot
    with store.connect() as observer:
        while True:
            current = observer.execute("PRAGMA data_version").fetchone()[0]
            if current != version:
                version = current
                snapshot = read_snapshot(store, cursor)
                if "items" in snapshot:
                    return snapshot | {"watch": True}
            remaining = deadline - monotonic()
            if remaining <= 0:
                return {"cursor": cursor, "watch": True}
            sleep(min(CHANGE_INTERVAL, remaining))


class RecordingPresence:
    def __init__(self, store):
        self.store = store
        self.changed = Condition()
        self.reports = {}
        with store.connect() as db:
            row = db.execute("SELECT changed_at,recording FROM watch_recording_state").fetchone()
        self.watch_transition = tuple(row) if row is not None else None

    def report(self, source, recording, *, valid_for=RECORDING_LEASE, changed_at=None):
        if type(recording) is not bool:
            raise ValueError("Send a boolean recording state.")
        if type(valid_for) not in (float, int) or not isfinite(valid_for) or valid_for < 0:
            raise ValueError("Send a nonnegative recording status lifetime.")
        if changed_at is not None and (type(changed_at) not in (float, int)
                                       or not 0 < changed_at <= time() + 60):
            raise ValueError("Send a valid watch recording change time.")
        with self.changed:
            if source == "watch" and not self._accept_watch(recording, changed_at):
                return {"saved": True}
            self.reports[source] = (recording, monotonic() + min(valid_for, RECORDING_LEASE))
            self.changed.notify_all()
        return {"saved": True}

    def _accept_watch(self, recording, changed_at):
        if changed_at is None:
            if self.watch_transition is not None:
                # A phone running an older release still reports the watch using leases.
                with self.store.connect() as db:
                    db.execute("DELETE FROM watch_recording_state")
                self.watch_transition = None
        elif self.watch_transition is None or changed_at > self.watch_transition[0]:
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO watch_recording_state VALUES(1,?,?)", (changed_at, recording))
            self.watch_transition = (changed_at, recording)
        else:
            return (changed_at, recording) == self.watch_transition
        return True

    def snapshot(self, source, now):
        if source == "watch" and self.watch_transition and self.watch_transition[1]:
            return {"state": "recording", "valid_for": RECORDING_LEASE, "until_changed": True}
        recording, expires = self.reports.get(source, (False, 0))
        remaining = expires - now
        if remaining <= 0:
            return {"state": "unknown", "valid_for": 0}
        return {"state": "recording" if recording else "idle", "valid_for": remaining}

    def read(self, source=None, previous=None):
        with self.changed:
            report, cursor, leases = self.snapshot_report(source)
            if previous == cursor:
                # A heartbeat renews the lease even when the microphone hasn't changed.
                self.changed.wait(min([MAX_WAIT] + [lease for lease in leases if lease > 0]))
                report, _, _ = self.snapshot_report(source)
            return report

    def snapshot_report(self, source):
        now = monotonic()
        if source:
            report = self.snapshot(source, now)
            return report, report["state"], [report["valid_for"]]
        recorders = {name: self.snapshot(name, now) for name in RECORDERS}
        cursor = ",".join(report["state"] for report in recorders.values())
        return {"recorders": recorders, "cursor": cursor}, cursor, [
            report["valid_for"] for report in recorders.values()
        ]
