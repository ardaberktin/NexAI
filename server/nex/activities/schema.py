"""One resumable recognition job and one observation per archived microphone clip."""

from datetime import datetime, timedelta

from ..recall_dates import time_key
from ..workspace_features import enabled

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_clips (
    recording_id TEXT PRIMARY KEY, started TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    due TEXT DEFAULT '', sound_due TEXT DEFAULT '', sounds TEXT, screen TEXT, category TEXT, basis TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS activity_due ON activity_clips(started DESC) WHERE due IS NOT NULL;
CREATE INDEX IF NOT EXISTS activity_sound_due ON activity_clips(started DESC) WHERE sound_due IS NOT NULL;
CREATE INDEX IF NOT EXISTS activity_history ON activity_clips(category,started DESC);
CREATE TABLE IF NOT EXISTS activity_screen_samples (observed TEXT PRIMARY KEY, summary TEXT);
DROP TRIGGER IF EXISTS activity_new_recording;
CREATE TRIGGER activity_new_recording AFTER INSERT ON recordings
WHEN NOT EXISTS (SELECT 1 FROM workspace_features WHERE name='activities' AND enabled=0) BEGIN
    INSERT INTO activity_clips(recording_id,started) VALUES(NEW.id,NEW.started);
END;
CREATE TRIGGER IF NOT EXISTS activity_transcript_changed AFTER UPDATE OF transcript ON recordings
WHEN OLD.transcript IS NOT NEW.transcript BEGIN
    UPDATE activity_clips SET version=version+1,due='',
        category=CASE WHEN basis='sound' AND sounds IS NOT NULL THEN category END
    WHERE recording_id=NEW.id;
END;
"""

SCREEN_FRESHNESS = timedelta(seconds=75)


def initialize(db):
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_clips'").fetchone()
    seed = "" if existing else (
        "INSERT OR IGNORE INTO activity_clips(recording_id,started) SELECT id,started FROM recordings;"
    )
    db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + seed + "\nCOMMIT;")


def interval(row):
    start = datetime.fromisoformat(row["started"])
    return start, start + timedelta(seconds=row["duration"])


def attach_screen(db, row):
    if not enabled(db, "activities"):
        return
    start, end = interval(row)
    sample = db.execute(
        """SELECT summary FROM activity_screen_samples WHERE observed>=? AND observed<?
           ORDER BY observed DESC LIMIT 1""", (time_key(start - SCREEN_FRESHNESS), time_key(end)),
    ).fetchone()
    summary = sample[0] if sample else None
    db.execute(
        """UPDATE activity_clips SET screen=?,version=version+1,due='',
               category=CASE WHEN basis!='screen' THEN category END
           WHERE recording_id=? AND screen IS NOT ?""", (summary, row["id"], summary),
    )
