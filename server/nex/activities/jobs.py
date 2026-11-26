"""Capture-time matching, optimistic recognition writes and restartable work selection."""

import json
from datetime import UTC, datetime, timedelta

from ..daybook_schedule import schedule_recordings
from ..live import PLAYBACK_LABELS
from ..recall_dates import time_key
from ..workspace_features import enabled, revision
from .recognition import from_sounds
from .schema import DEVICE_FRESHNESS, SCREEN_FRESHNESS, attach_device_context, attach_screen, interval

SCREEN_RETENTION = timedelta(minutes=20)
SOUND_CONTEXT_WINDOW = timedelta(seconds=45)


def expire_samples(db, anchor):
    cutoff = time_key(anchor - SCREEN_RETENTION)
    db.execute("DELETE FROM activity_screen_samples WHERE observed<?", (cutoff,))
    db.execute("DELETE FROM activity_device_samples WHERE observed<?", (cutoff,))


def record_screen(store, observed, summary):
    anchor = datetime.now(UTC)
    if not anchor - SCREEN_RETENTION < observed <= anchor + timedelta(minutes=2):
        return
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not enabled(db, "activities"):
            return
        expire_samples(db, anchor)
        db.execute("INSERT OR REPLACE INTO activity_screen_samples VALUES(?,?)", (time_key(observed), summary))
        # A caption can finish after the corresponding clips have arrived.
        rows = db.execute("SELECT id,started,duration FROM recordings WHERE started>=? AND started<=?",
                          (time_key(observed - timedelta(seconds=65)), time_key(observed + SCREEN_FRESHNESS)))
        for row in rows:
            attach_screen(db, row)


def record_device_context(store, device, observed, app, media_playing):
    anchor = datetime.now(UTC)
    if not anchor - SCREEN_RETENTION < observed <= anchor + timedelta(minutes=2):
        return
    payload = None
    if media_playing or app:
        payload = json.dumps({"media_playing": bool(media_playing), **({"app": app} if app else {})},
                             ensure_ascii=False)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not enabled(db, "activities"):
            return
        expire_samples(db, anchor)
        db.execute("INSERT OR REPLACE INTO activity_device_samples VALUES(?,?,?)",
                   (device, time_key(observed), payload))
        rows = db.execute("SELECT id,device,started,duration FROM recordings WHERE started>=? AND started<=?",
                          (time_key(observed - timedelta(seconds=65)), time_key(observed + DEVICE_FRESHNESS)))
        for row in rows:
            if row["device"] == device or device == "mac":
                attach_device_context(db, row)


class ActivityJobs:
    def __init__(self, store):
        self.store = store

    def next(self, *, audio=False, wait_for_sounds=False):
        condition = "a.sound_due<=? AND r.audio_state='available'" if audio else "a.due<=?"
        if wait_for_sounds:
            condition += (" AND (a.sounds IS NOT NULL OR r.audio_state!='available'"
                          " OR a.screen IS NOT NULL OR a.device_context IS NOT NULL"
                          " OR COALESCE(r.transcript,'')!='')")
        with self.store.connect() as db:
            feature_revision = revision(db, "activities")
            if feature_revision is None:
                return None
            return db.execute(
                f"""SELECT ? AS feature_revision,a.*,r.device,r.mode,r.transcript,r.audio_state,r.duration,r.time_zone
                   FROM activity_clips a
                   JOIN recordings r ON r.id=a.recording_id WHERE {condition}
                   ORDER BY a.started DESC LIMIT 1""", (feature_revision, time_key(datetime.now(UTC))),
            ).fetchone()

    def nearby_sounds(self, job):
        start = datetime.fromisoformat(job["started"])
        lower = start - SOUND_CONTEXT_WINDOW
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT r.started,r.duration,a.sounds FROM activity_clips a
                   JOIN recordings r ON r.id=a.recording_id
                   WHERE r.device=? AND r.started>=? AND r.started<? AND json_array_length(a.sounds)>0
                   ORDER BY r.started DESC,r.id DESC LIMIT 4""",
                (job["device"], time_key(lower - timedelta(seconds=65)), job["started"]),
            )
            context = []
            for row in rows:
                _, end = interval(row)
                if end < lower or end > start:
                    continue
                context.append({"seconds_before": round((start - end).total_seconds()),
                                "labels": json.loads(row["sounds"])})
            return context

    def save_sounds(self, job, sounds, *, audio_context=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if revision(db, "activities") != job["feature_revision"]:
                return
            latest = db.execute(
                """SELECT a.*,r.device,r.duration,r.time_zone,r.mode,r.transcript FROM activity_clips a
                   JOIN recordings r ON r.id=a.recording_id WHERE recording_id=?""", (job["recording_id"],),
            ).fetchone()
            if latest is None:
                return
            profile = json.dumps(audio_context, ensure_ascii=False) if audio_context else None
            db.execute(
                "UPDATE activity_clips SET sounds=?,audio_context=?,sound_due=NULL,version=version+1 WHERE recording_id=?",
                (json.dumps(sounds), profile, job["recording_id"]),
            )
            if latest["category"] is None:
                self.write(db, latest, from_sounds(sounds, latest["mode"], latest["transcript"]), None)
            if sounds or latest["transcript"] or latest["screen"] or latest["device_context"]:
                db.execute("UPDATE activity_clips SET due='' WHERE recording_id=?", (job["recording_id"],))
            if PLAYBACK_LABELS.intersection(sounds or ()):
                _, end = interval(latest)
                db.execute(
                    """UPDATE activity_clips SET due='',version=version+1
                       WHERE recording_id IN (
                           SELECT a.recording_id FROM activity_clips a
                           JOIN recordings r ON r.id=a.recording_id
                           WHERE r.device=? AND r.started>? AND r.started<=?
                             AND (a.category IS NULL OR a.category IN ('unknown','speech','conversation'))
                       )""",
                    (latest["device"], latest["started"], time_key(end + SOUND_CONTEXT_WINDOW)),
                )

    def defer_audio(self, job, due):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if revision(db, "activities") == job["feature_revision"]:
                db.execute("UPDATE activity_clips SET sound_due=? WHERE recording_id=?", (due, job["recording_id"]))

    def save(self, job, observation, *, due=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if revision(db, "activities") != job["feature_revision"]:
                return
            if db.execute("SELECT 1 FROM activity_clips WHERE recording_id=? AND version=?",
                          (job["recording_id"], job["version"])).fetchone():
                self.write(db, job, observation, due)

    @staticmethod
    def write(db, job, observation, due):
        values = observation.category, observation.basis, observation.detail
        db.execute("UPDATE activity_clips SET category=?,basis=?,detail=?,due=? WHERE recording_id=?",
                   (*values, due, job["recording_id"]))
        if tuple(job[key] for key in ("category", "basis", "detail")) != values:
            schedule_recordings(db, [job])

    def expire_screens(self):
        with self.store.connect() as db:
            expire_samples(db, datetime.now(UTC))
