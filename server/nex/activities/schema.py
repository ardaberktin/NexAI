"""One resumable recognition job and one observation per archived microphone clip."""

import json
from datetime import datetime, timedelta

from ..recall_dates import time_key
from ..workspace_features import enabled

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_clips (
    recording_id TEXT PRIMARY KEY, started TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    due TEXT DEFAULT '', sound_due TEXT DEFAULT '', sounds TEXT, screen TEXT, category TEXT, basis TEXT, detail TEXT,
    title TEXT, audio_context TEXT, device_context TEXT, clip_media_playing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS activity_due ON activity_clips(started DESC) WHERE due IS NOT NULL;
CREATE INDEX IF NOT EXISTS activity_sound_due ON activity_clips(started DESC) WHERE sound_due IS NOT NULL;
CREATE INDEX IF NOT EXISTS activity_history ON activity_clips(category,started DESC);
CREATE TABLE IF NOT EXISTS activity_screen_samples (observed TEXT PRIMARY KEY, summary TEXT);
CREATE TABLE IF NOT EXISTS activity_device_samples (
    device TEXT NOT NULL, observed TEXT NOT NULL, summary TEXT, PRIMARY KEY(device, observed)
);
CREATE TABLE IF NOT EXISTS activity_migrations (name TEXT PRIMARY KEY);
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

REQUEUE_SOUND_OBSERVATIONS = """
UPDATE activity_clips SET due='',version=version+1
WHERE category='unknown' AND due IS NULL AND json_array_length(sounds)>0
  AND COALESCE(screen,'')='' AND EXISTS (
      SELECT 1 FROM recordings WHERE id=activity_clips.recording_id AND COALESCE(transcript,'')=''
  )
  AND NOT EXISTS (SELECT 1 FROM activity_migrations WHERE name='numbered_sound_evidence');
INSERT OR IGNORE INTO activity_migrations VALUES('numbered_sound_evidence');
"""

SCREEN_FRESHNESS = timedelta(seconds=95)
DEVICE_FRESHNESS = timedelta(seconds=95)


def initialize(db):
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_clips'").fetchone()
    seed = "" if existing else (
        "INSERT OR IGNORE INTO activity_clips(recording_id,started) SELECT id,started FROM recordings;"
    )
    db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + seed + REQUEUE_SOUND_OBSERVATIONS + "\nCOMMIT;")
    with db:
        columns = {column["name"] for column in db.execute("PRAGMA table_info(activity_clips)")}
        if "title" not in columns:
            db.execute("ALTER TABLE activity_clips ADD COLUMN title TEXT")
        if "audio_context" not in columns:
            db.execute("ALTER TABLE activity_clips ADD COLUMN audio_context TEXT")
        if "device_context" not in columns:
            db.execute("ALTER TABLE activity_clips ADD COLUMN device_context TEXT")
        if "clip_media_playing" not in columns:
            db.execute("ALTER TABLE activity_clips ADD COLUMN clip_media_playing INTEGER NOT NULL DEFAULT 0")


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


def _device_sample(db, device, start, end):
    sample = db.execute(
        """SELECT summary FROM activity_device_samples WHERE device=? AND observed>=? AND observed<?
           ORDER BY observed DESC LIMIT 1""", (device, time_key(start - DEVICE_FRESHNESS), time_key(end)),
    ).fetchone()
    if not sample or not sample[0]:
        return None
    try:
        parsed = json.loads(sample[0])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def attach_device_context(db, row, *, media_playing=None):
    if not enabled(db, "activities"):
        return
    if media_playing is True:
        db.execute("UPDATE activity_clips SET clip_media_playing=1 WHERE recording_id=?", (row["id"],))
    clip = db.execute("SELECT clip_media_playing FROM activity_clips WHERE recording_id=?", (row["id"],)).fetchone()
    if clip is None:
        return
    clip_playing = bool(clip["clip_media_playing"])
    start, end = interval(row)
    device = row["device"]
    own = _device_sample(db, device, start, end)
    context = None
    if clip_playing:
        context = {"media_playing": True, "device": device}
        if own and own.get("app"):
            context["app"] = own["app"]
    elif own and own.get("media_playing"):
        context = {"media_playing": True, "device": device}
        if own.get("app"):
            context["app"] = own["app"]
    elif device != "mac":
        mac = _device_sample(db, "mac", start, end)
        if mac and mac.get("media_playing"):
            context = {"media_playing": True, "device": "mac"}
            if mac.get("app"):
                context["app"] = mac["app"]
    encoded = json.dumps(context, ensure_ascii=False) if context else None
    db.execute(
        """UPDATE activity_clips SET device_context=?,version=version+1,due='',
               category=CASE WHEN basis!='device' THEN category END
           WHERE recording_id=? AND device_context IS NOT ?""", (encoded, row["id"], encoded),
    )
