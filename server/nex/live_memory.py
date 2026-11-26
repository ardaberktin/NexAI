"""Shared archive operations for authenticated live turns."""

import json
from pathlib import Path

from .conversation_recall import time_key
from .personal_learning import enqueue_context
from .store import Store, now


def read_result(db, row):
    return db.execute("SELECT * FROM live_turn_results WHERE id=? AND device=?",
                      (row["id"], row["device"])).fetchone()


def attach_reply(store, db, row, reply):
    updated = db.execute("UPDATE recordings SET reply=? WHERE id=? AND reply IS NULL AND live_handled=1",
                         (json.dumps(reply, ensure_ascii=False), row["id"]))
    if updated.rowcount:
        sources = list(dict.fromkeys([row["id"], *reply["sources"]]))
        store.add_memory(db, "a-" + row["id"], row["started"], "answer", row["device"],
                         reply["text"], sources, reply["engine"], recall_cursor=reply.get("recall_cursor"))


class LiveMemory(Store):
    """Reuse archive queries and isolated result writes without starting an archive worker."""

    def __init__(self, root):
        self.root = Path(root)

    def save_spoken_question(self, identifier, device, started, text):
        with self.connect() as db:
            self.add_memory(db, "q-" + identifier, time_key(started), "question", device, text)

    def observe_dialogue(self, identifier, started, text):
        with self.connect() as db:
            enqueue_context(db, identifier, "dialogue", text, time_key(started))

    def save_result(self, identifier, device, *, transcript=None, reply=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.stage_result(db, identifier, device, transcript, reply)

    def save_turn_reply(self, turn, result):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if turn.archived:
                result = self.stage_result(db, turn.id, turn.device, None, result)
            if turn.journal is None:
                started = time_key(turn.started)
                self.add_memory(db, "q-" + turn.id, started, "question", turn.device, turn.text)
                # A reconnect can arrive before the phone finishes uploading the clip.
                sources = list(dict.fromkeys([turn.id, *result["sources"]])) if turn.archived else result["sources"]
                self.add_memory(db, "a-" + turn.id, started if turn.archived else now(), "answer", turn.device,
                                result["text"], sources, result["engine"], recall_cursor=result.get("recall_cursor"))

    def stage_result(self, db, identifier, device, transcript, reply):
        db.execute(
            """INSERT INTO live_turn_results(id, device, transcript, reply, updated) VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       transcript=COALESCE(excluded.transcript,live_turn_results.transcript),
                       reply=COALESCE(live_turn_results.reply,excluded.reply), updated=excluded.updated
                   WHERE live_turn_results.device=excluded.device""",
            (identifier, device, transcript, json.dumps(reply, ensure_ascii=False) if reply else None, now()),
        )
        if reply:
            saved = db.execute("SELECT reply FROM live_turn_results WHERE id=? AND device=?",
                               (identifier, device)).fetchone()
            if saved is None:
                raise ValueError("This voice turn belongs to another device.")
            reply = json.loads(saved["reply"])
            row = db.execute("SELECT * FROM recordings WHERE id=? AND device=? AND live_handled=1",
                             (identifier, device)).fetchone()
            if row and row["state"] in {"ready", "quiet"}:
                attach_reply(self, db, row, reply)
        # Only unclaimed results need staging. The recording owns the durable outcome.
        db.execute("""DELETE FROM live_turn_results
                      WHERE julianday('now')-julianday(updated)>7""")
        return reply
