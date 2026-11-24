import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from nex.audio_archive import GIB
from nex.store import ArchiveFull, Store


class AudioArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root, 0, 8192)
        self.at = datetime(2026, 11, 16, 12, 30, tzinfo=UTC)
        self.media = b"private audio fixture"

    def clip(self, days, state="ready", text="We decided to check backups on Friday.", index=True):
        identifier = str(uuid4())
        started = (self.at - timedelta(days=days)).isoformat(timespec="milliseconds")
        self.store.save_recording(identifier, "mac", started, 1.0, "ambient", False, "en", self.media)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state=?,transcript=? WHERE id=?", (state, text, identifier))
            if text and index:
                self.store.add_memory(db, identifier, started, "recording", "mac", text)
                self.store.add_memory(db, "n-" + identifier, started, "note", "mac",
                                      "Check backups on Friday.", [identifier], "excerpt")
        return identifier, started

    def set_retention(self, days):
        return self.store.archive.configure({"audio_limit_gib": 1, "retention_days": days})

    def test_keeping_audio_is_default_and_archive_capacity_is_configurable(self):
        clip, _ = self.clip(45)
        self.assertEqual(self.store.status()["audio_limit_bytes"], 8192)
        self.assertEqual(self.store.archive.settings()["retention_days"], 0)
        self.assertEqual(self.store.archive.expire(at=self.at), 0)
        self.assertTrue(self.store.audio_path(clip).is_file())
        with self.assertRaises(ValueError):
            self.store.archive.configure({"audio_limit_gib": True, "retention_days": 0})
        self.assertEqual(self.set_retention(0)["audio_limit_bytes"], GIB)
        self.store.archive.configure({"audio_limit_gib": None, "retention_days": 0})
        reopened = Store(self.root, 100 * GIB, 8192)
        with patch("nex.audio_archive.disk_usage", return_value=SimpleNamespace(free=100 * GIB + 2000)):
            self.assertEqual(reopened.status()["remaining_bytes"], 2000)
            self.assertIsNone(reopened.status()["audio_limit_bytes"])
            with self.assertRaises(ArchiveFull):
                reopened.save_recording(str(uuid4()), "mac", self.at.isoformat(), 1, "ambient", False, "en",
                                        b"x" * 2001)

    def test_opt_in_preserves_indexed_text_notes_and_duplicate_retry(self):
        expired, started = self.clip(31)
        boundary, _ = self.clip(30)
        pending, _ = self.clip(42, "awaiting_transcription", None)
        unindexed, _ = self.clip(41, index=False)
        quiet, _ = self.clip(40, "quiet", "")
        self.set_retention(30)
        self.assertEqual(self.store.archive.expire(at=self.at), 2)
        self.assertEqual(self.store.status()["audio_bytes"], 3 * len(self.media))
        for key in (expired, quiet):
            self.assertFalse(self.store.audio_path(key).exists())
            self.assertFalse(self.store.recording(key)["audio_available"])
        for key in (boundary, pending, unindexed):
            self.assertTrue(self.store.audio_path(key).is_file())
        correct = self.store.source(expired)
        self.assertIn("backups", correct["text"])
        self.assertFalse(correct["audio_available"])
        self.assertEqual(self.store.source("n-" + expired)["sources"], [expired])
        matches = self.store.search("backups")
        self.assertFalse(next(item for item in matches if item["id"] == expired)["audio_available"])
        existing, fresh = self.store.save_recording(
            expired, "mac", started, 1.0, "ambient", False, "en", self.media,
        )
        self.assertFalse(fresh)
        self.assertFalse(existing["audio_available"])
        self.assertFalse(self.store.audio_path(expired).exists())

    def test_interrupted_expiration_is_resumable_and_accounts_for_bytes_once(self):
        clip, _ = self.clip(120)
        self.set_retention(30)
        self.assertEqual(self.store.archive.claim(self.at, 1), [clip])
        self.store.audio_path(clip).unlink()
        self.assertEqual(self.store.status()["audio_bytes"], len(self.media))
        reopened = Store(self.root, 0, 8192)
        self.assertEqual(reopened.archive.expire(at=self.at), 1)
        self.assertEqual(reopened.archive.expire(at=self.at), 0)
        self.assertEqual(reopened.status()["audio_bytes"], 0)
        self.assertIsNotNone(reopened.recording(clip)["audio_removed"])

    def test_offload_moves_oldest_ssd_audio_and_resumes_across_restart(self):
        existing, _ = self.clip(60)
        ssd = self.root / "ssd"
        self.store = Store(self.root, 0, 8192, ssd_audio=ssd)
        size = len(self.media)
        with patch.multiple("nex.audio_archive", SSD_LIMIT=4 * size, SSD_TARGET=size, SSD_RESERVE=0):
            new, _ = self.clip(0)
            oldest, _ = self.clip(4)
            middle, _ = self.clip(2)
            self.assertEqual(self.store.archive.offload(), 0)
            older, _ = self.clip(3)
            self.assertEqual(self.store.status()["ssd_audio_bytes"], 4 * size)
            self.assertFalse(self.store.audio_path(existing).is_symlink())
            self.assertEqual(self.store.archive.offload(limit=1), 1)
            self.assertTrue(self.store.status()["ssd_offloading"])

            # Simulate a restart after the file move but before its database update.
            with self.store.connect() as db:
                sha = db.execute("SELECT sha FROM recordings WHERE id=?", (older,)).fetchone()[0]
            self.store.archive.files.offload(older, sha)
            self.store = Store(self.root, 0, 8192, ssd_audio=ssd)
            self.assertEqual(self.store.archive.offload(), 2)
            self.assertFalse(self.store.status()["ssd_offloading"])
            self.assertEqual(self.store.status()["ssd_audio_bytes"], size)
            self.assertEqual(self.store.status()["data_audio_bytes"], 4 * size)
            for clip in (oldest, older, middle):
                self.assertFalse(self.store.audio_path(clip).is_symlink())
                self.assertEqual(self.store.audio_path(clip).read_bytes(), self.media)
            self.assertTrue(self.store.audio_path(new).is_symlink())
            self.assertEqual(list(ssd.glob("*.m4a")), [ssd / (new + ".m4a")])

    def test_ssd_reserve_and_offload_failure_leave_audio_available(self):
        ssd = self.root / "ssd"
        self.store = Store(self.root, 0, 8192, ssd_audio=ssd)
        size = len(self.media)
        with patch.multiple("nex.audio_archive", SSD_LIMIT=2 * size, SSD_TARGET=size, SSD_RESERVE=0):
            oldest, _ = self.clip(60)
            newer, _ = self.clip(50)
            with patch("nex.audio_archive.disk_usage", return_value=SimpleNamespace(free=0)):
                self.assertEqual(self.store.archive.offload(), 0)
            self.assertTrue(self.store.audio_path(oldest).is_symlink())
            self.assertEqual(self.store.audio_path(oldest).read_bytes(), self.media)
            direct, _ = self.clip(40)
            self.assertFalse(self.store.audio_path(direct).is_symlink())
            with patch.object(self.store.archive.files, "offload", side_effect=OSError), self.assertLogs():
                self.assertEqual(self.store.archive.offload(), 0)
            self.assertTrue(self.store.audio_path(newer).is_file())
            self.set_retention(45)
            self.assertEqual(self.store.archive.expire(at=self.at), 2)
            self.assertEqual(self.store.status()["ssd_audio_bytes"], 0)
            self.assertEqual(list(ssd.glob("*.m4a")), [])

        with patch("nex.audio_archive.SSD_RESERVE", 10**18):
            fallback, _ = self.clip(1)
            self.assertFalse(self.store.audio_path(fallback).is_symlink())


if __name__ == "__main__":
    unittest.main()
