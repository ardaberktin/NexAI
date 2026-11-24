import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

from nex.daybook import Daybook, DayContext
from nex.daybook_briefing import Briefings, pack, parse_briefing, passages
from nex.store import Store, now
from nex.workspace_features import update


class DaybookTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), min_free=0, quota=2**30)
        self.daybook = Daybook(self.store)
        self.context = DayContext.parse("2025-11-02", "America/Vancouver")

    def save(self, started, text, device="iphone", state="ready", mode="ambient"):
        identifier = str(uuid4())
        self.store.save_recording(identifier, device, started, 20, mode, False, "en", b"fixture",
                                  zone=self.context.zone)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state=?,transcript=?,updated=? WHERE id=?",
                       (state, text, now(), identifier))
        return identifier

    def test_local_day_groups_speech_across_short_pauses_and_respects_device_and_recorded_gaps(self):
        start, finish = map(datetime.fromisoformat, self.context.bounds)
        self.assertEqual(finish - start, timedelta(hours=25))

        def stamp(seconds):
            return (start + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")

        previous = self.save(stamp(-1), "Prior day")
        first = self.save(stamp(1), "We booked Friday.")
        other_device = self.save(stamp(25), "Another microphone", "mac")
        second = self.save(stamp(35), "The appointment is at noon.")
        after_gap = self.save(stamp(60), "Separate topic")
        self.save(stamp(80), None, state="queued")
        self.save(stamp(100), "Can you repeat that?", mode="voice")
        self.save(finish.isoformat(timespec="milliseconds"), "Next day")
        with self.store.connect() as db:
            self.store.add_memory(db, "g-" + str(uuid4()), stamp(54), "gap", "iphone", "Recording paused.")

        result = self.daybook.overview(self.context)
        self.assertEqual(result["pending"], 1)
        self.assertEqual([(group["id"], group["count"]) for group in result["conversations"][:3]],
                         [(first, 2), (other_device, 1), (after_gap, 1)])
        scope = DayContext.parse(self.context.day, self.context.zone, second, "The appointment")
        evidence = self.daybook.evidence(scope, "What time was it?")
        self.assertEqual([row["id"] for row in evidence], [second, first])
        self.assertIsNone(self.daybook.conversation(DayContext(self.context.day, self.context.zone, previous)))

    def test_backfill_and_merge_use_all_batches_with_only_known_sources(self):
        start = self.context.bounds[0]
        first = self.save(start, "A long transcript " * 440)
        second = self.save(start, "A different clip is here.")
        worker = Briefings(self.store, AsyncMock())
        worker.discover()
        with self.store.connect() as db:
            report = db.execute("SELECT day,zone,version FROM daybook_reports").fetchone()
            self.assertEqual((report["day"], report["zone"]), (self.context.day, self.context.zone))
        rows = self.daybook.recordings(self.context)
        self.assertGreater(len(list(pack(passages(rows)))), 1)

        async def generate(messages, _):
            materials = json.loads(messages[1]["content"])
            rollup = "highlights" in materials[0]
            source = materials[0]["highlights"][0]["source"] if rollup else materials[0]["source"]
            return json.dumps({"summary": "A concrete overview.", "highlights": [
                {"kind": "follow_up", "source": source, "text": "A sourced detail."},
                {"kind": "decision", "source": str(uuid4()), "text": "Unrecognized citation."},
            ]})

        worker.generate.side_effect = generate
        briefing = asyncio.run(worker.build(rows))
        self.assertGreater(worker.generate.await_count, 2)
        self.assertEqual(len(briefing["highlights"]), 1)
        self.assertIn(briefing["highlights"][0]["source"], {first, second})
        worker.finish(report, briefing)
        self.assertEqual(self.daybook.overview(self.context)["briefing"], briefing)
        call_count = worker.generate.await_count
        asyncio.run(worker.build(rows))
        self.assertEqual(worker.generate.await_count, call_count)
        self.assertIsNone(parse_briefing("Could not complete", {first}))

        later = (datetime.fromisoformat(now()) + timedelta(seconds=1)).isoformat(timespec="milliseconds")
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET transcript=?,updated=? WHERE id=?", ("New corrected transcript.", later, first))
        worker.discover()
        self.assertTrue(self.daybook.overview(self.context)["updating"])

    def test_daybook_pauses_and_regenerates_briefings_when_activities_change(self):
        self.save(self.context.bounds[0], "We booked Friday.")
        self.daybook.overview(self.context)
        worker = Briefings(self.store, AsyncMock())
        original = {"summary": "We booked Friday and played a game.", "highlights": []}
        worker.finish(worker.next_report(), original)
        self.assertEqual(self.daybook.briefing(self.context), original)

        update(self.store, "activities", False)
        self.assertIsNone(self.daybook.briefing(self.context))
        self.assertTrue(self.daybook.overview(self.context)["updating"])
        stale_job = worker.next_report()
        update(self.store, "daybook", False)
        self.assertIsNone(worker.next_report())
        self.assertFalse(worker.discover())

        update(self.store, "daybook", True)
        worker.finish(stale_job, original, with_activities=False)
        self.assertIsNone(self.daybook.briefing(self.context))
        revised = {"summary": "We booked Friday.", "highlights": []}
        worker.finish(worker.next_report(), revised, with_activities=False)
        self.assertEqual(self.daybook.briefing(self.context), revised)


if __name__ == "__main__":
    unittest.main()
