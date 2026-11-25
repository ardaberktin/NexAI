import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from shutil import disk_usage

from .audio_files import AudioFiles, sync_directory

logger = logging.getLogger(__name__)


GB = 10**9
GIB = 2**30
BATCH_SIZE = 128
SSD_LIMIT = 20 * GB
SSD_TARGET = 5 * GB
SSD_RESERVE = 10 * GB


class AudioArchive:
    def __init__(self, connect, directory, minimum_free, ssd=None):
        self.connect = connect
        self.directory = directory
        self.files = AudioFiles(directory, ssd)
        self.minimum_free = minimum_free

    @staticmethod
    def initialize(db, quota):
        columns = {column["name"] for column in db.execute("PRAGMA table_info(recordings)")}
        if "audio_state" not in columns:
            db.execute("ALTER TABLE recordings ADD COLUMN audio_state TEXT NOT NULL DEFAULT 'available' "
                       "CHECK(audio_state IN ('available','purging','expired'))")
        if "audio_removed" not in columns:
            db.execute("ALTER TABLE recordings ADD COLUMN audio_removed TEXT")
        if "audio_on_ssd" not in columns:
            db.execute("ALTER TABLE recordings ADD COLUMN audio_on_ssd INTEGER NOT NULL DEFAULT 0")
        db.execute(
            """CREATE TABLE IF NOT EXISTS archive_policy(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), audio_limit_bytes INTEGER,
                retention_days INTEGER NOT NULL
            )"""
        )
        db.execute("INSERT OR IGNORE INTO archive_policy VALUES (1,?,0)", (quota,))
        db.execute("CREATE TABLE IF NOT EXISTS archive_usage (singleton INTEGER PRIMARY KEY "
                   "CHECK(singleton=1), bytes INTEGER NOT NULL)")
        db.execute("INSERT OR IGNORE INTO archive_usage(singleton,bytes) SELECT 1,COALESCE(SUM(size),0) "
                   "FROM recordings WHERE audio_state!='expired'")
        usage_columns = {column["name"] for column in db.execute("PRAGMA table_info(archive_usage)")}
        if "ssd_bytes" not in usage_columns:
            db.execute("ALTER TABLE archive_usage ADD COLUMN ssd_bytes INTEGER NOT NULL DEFAULT 0")
        if "ssd_draining" not in usage_columns:
            db.execute("ALTER TABLE archive_usage ADD COLUMN ssd_draining INTEGER NOT NULL DEFAULT 0")
        db.execute("""CREATE TRIGGER IF NOT EXISTS recording_bytes AFTER INSERT ON recordings BEGIN
            UPDATE archive_usage SET bytes=bytes+new.size WHERE singleton=1;
        END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS recording_ssd_insert AFTER INSERT ON recordings
            WHEN new.audio_on_ssd=1 BEGIN
            UPDATE archive_usage SET ssd_bytes=ssd_bytes+new.size WHERE singleton=1;
        END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS recording_ssd_update AFTER UPDATE OF audio_on_ssd ON recordings
            WHEN new.audio_on_ssd<>old.audio_on_ssd BEGIN
            UPDATE archive_usage SET ssd_bytes=ssd_bytes+(new.audio_on_ssd-old.audio_on_ssd)*new.size WHERE singleton=1;
        END""")
        db.execute("""CREATE INDEX IF NOT EXISTS recordings_ssd_oldest ON recordings(started,id)
            WHERE audio_on_ssd=1 AND audio_state='available'""")
        db.execute("""CREATE INDEX IF NOT EXISTS recordings_retention_due ON recordings(started,id)
            WHERE audio_state='available' AND state IN ('ready','quiet') AND transcript IS NOT NULL""")
        db.execute("""CREATE INDEX IF NOT EXISTS recordings_retention_pending ON recordings(started,id)
            WHERE audio_state='purging'""")

    def path(self, identifier):
        return self.files.path(identifier)

    def capacity(self, db):
        rule = db.execute("SELECT audio_limit_bytes,retention_days FROM archive_policy").fetchone()
        used, ssd_used, draining = db.execute(
            "SELECT bytes,ssd_bytes,ssd_draining FROM archive_usage WHERE singleton=1"
        ).fetchone()
        usable = disk_usage(self.directory).free - self.minimum_free - ssd_used
        limit = rule["audio_limit_bytes"]
        return {
            "audio_bytes": used, "audio_limit_bytes": limit, "retention_days": rule["retention_days"],
            "disk_reserve_bytes": self.minimum_free,
            "remaining_bytes": max(0, min(usable, limit - used) if limit is not None else usable),
            "ssd_audio_bytes": ssd_used, "data_audio_bytes": used - ssd_used,
            "ssd_limit_bytes": SSD_LIMIT if self.files.ssd is not None else None,
            "ssd_target_bytes": SSD_TARGET if self.files.ssd is not None else None,
            "ssd_offloading": bool(draining),
        }

    def write(self, identifier, audio, capacity):
        ssd = self.files.ssd
        if ssd is not None and capacity["ssd_audio_bytes"] < SSD_LIMIT:
            try:
                if disk_usage(ssd).free - SSD_RESERVE >= len(audio):
                    self.files.write(identifier, audio, on_ssd=True)
                    return 1
            except OSError as exc:
                logger.warning("SSD audio write failed, using the data drive (%s)", type(exc).__name__)
        self.files.write(identifier, audio, on_ssd=False)
        return 0

    def start_offload(self, limit):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used, draining = db.execute("SELECT ssd_bytes,ssd_draining FROM archive_usage").fetchone()
            if used <= SSD_TARGET:
                if draining:
                    db.execute("UPDATE archive_usage SET ssd_draining=0 WHERE singleton=1")
                return []
            if not draining:
                if used < SSD_LIMIT:
                    return []
                db.execute("UPDATE archive_usage SET ssd_draining=1 WHERE singleton=1")
            return [row[0] for row in db.execute(
                """SELECT id FROM recordings WHERE audio_on_ssd=1 AND audio_state='available'
                   ORDER BY started,id LIMIT ?""", (limit,),
            )]

    def offload(self, limit=BATCH_SIZE):
        if self.files.ssd is None:
            return 0
        moved = 0
        for identifier in self.start_offload(limit):
            try:
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT ssd_bytes FROM archive_usage").fetchone()[0] <= SSD_TARGET:
                        db.execute("UPDATE archive_usage SET ssd_draining=0 WHERE singleton=1")
                        break
                    row = db.execute(
                        """SELECT sha,size FROM recordings WHERE id=? AND audio_on_ssd=1
                           AND audio_state='available'""", (identifier,),
                    ).fetchone()
                    if row is None:
                        continue
                    canonical = self.path(identifier)
                    needs_copy = canonical.is_symlink() or not canonical.is_file()
                    if needs_copy and disk_usage(self.directory).free - self.minimum_free < row["size"]:
                        break
                    self.files.offload(identifier, row["sha"])
                    db.execute("UPDATE recordings SET audio_on_ssd=0 WHERE id=?", (identifier,))
                    db.execute("UPDATE archive_usage SET ssd_draining=0 WHERE ssd_bytes<=?", (SSD_TARGET,))
                    moved += 1
            except OSError as exc:
                logger.warning("SSD audio offload could not finish a clip (%s)", type(exc).__name__)
        return moved

    def settings(self):
        with self.connect() as db:
            return self.capacity(db)

    def configure(self, options):
        if options.keys() != {"audio_limit_gib", "retention_days"}:
            raise ValueError("Supply the audio limit and retention days.")
        limit, days = options["audio_limit_gib"], options["retention_days"]
        limit_bytes = self.limit_bytes(limit)
        if type(days) is not int or not 0 <= days <= 36500:
            raise ValueError("Enter a retention period in whole days, or 0 to keep all audio.")
        with self.connect() as db:
            db.execute("UPDATE archive_policy SET audio_limit_bytes=?,retention_days=? WHERE singleton=1",
                       (limit_bytes, days))
            return self.capacity(db)

    @staticmethod
    def limit_bytes(limit):
        if limit is not None and (type(limit) is not int or not 1 <= limit < 2**23):
            raise ValueError("Enter a positive, whole GiB limit or leave the limit blank.")
        return None if limit is None else limit * GIB

    def set_limit(self, limit):
        limit_bytes = self.limit_bytes(limit)
        with self.connect() as db:
            db.execute("UPDATE archive_policy SET audio_limit_bytes=? WHERE singleton=1", (limit_bytes,))
            return self.capacity(db)

    def claim(self, at, limit):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [row[0] for row in db.execute(
                "SELECT id FROM recordings WHERE audio_state='purging' ORDER BY started,id LIMIT ?", (limit,)
            )]
            days = db.execute("SELECT retention_days FROM archive_policy").fetchone()[0]
            if days and len(ids) < limit:
                cutoff = (at - timedelta(days=days)).astimezone(UTC).isoformat(timespec="milliseconds")
                due = [row[0] for row in db.execute(
                    """SELECT id FROM recordings WHERE audio_state='available' AND state IN ('ready','quiet')
                       AND transcript IS NOT NULL AND started < ?
                       AND ((state='quiet' AND transcript='') OR EXISTS (
                           SELECT 1 FROM memory m WHERE m.id=recordings.id
                           AND m.kind='recording' AND m.body=recordings.transcript
                       ))
                       ORDER BY started,id LIMIT ?""",
                    (cutoff, limit - len(ids)),
                )]
                db.executemany("UPDATE recordings SET audio_state='purging' WHERE id=?", ((key,) for key in due))
                ids.extend(due)
            return ids

    def expire(self, at=None, limit=BATCH_SIZE):
        at = at or datetime.now(UTC)
        removed = []
        for identifier in self.claim(at, limit):
            try:
                self.files.remove(identifier)
            except OSError as exc:
                logger.warning("Audio retention could not unlink a clip (%s)", type(exc).__name__)
            else:
                removed.append(identifier)
        if not removed:
            return 0

        # Persist the directory changes before freeing quota. An interrupted batch can be resumed.
        sync_directory(self.directory)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stamp = at.astimezone(UTC).isoformat(timespec="milliseconds")
            released = 0
            count = 0
            for identifier in removed:
                result = db.execute(
                    """UPDATE recordings SET audio_state='expired',audio_removed=?,audio_on_ssd=0
                       WHERE id=? AND audio_state='purging' RETURNING size""", (stamp, identifier),
                ).fetchone()
                if result:
                    released += result[0]
                    count += 1
            db.execute("UPDATE archive_usage SET bytes=bytes-? WHERE singleton=1", (released,))
            return count

    def maintain(self, stop):
        while not stop.is_set():
            try:
                count = self.expire()
                count = max(count, self.offload())
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Audio archive maintenance failed (%s)", type(exc).__name__)
                count = 0
            stop.wait(0.25 if count == BATCH_SIZE else 20)
