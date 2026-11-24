import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conversation_fixtures import model, unlock
from nex.assistant import Assistant
from nex.personal_files import PersonalConflict, PersonalFiles, time_key
from nex.personal_learning import PersonalLearning, enqueue_context, parse_updates
from nex.privacy.public import chat
from nex.store import Store


class PersonalContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name, 0, 1_048_576)
        self.files = PersonalFiles(self.store.root)

    def test_reads_real_shared_files_and_rejects_a_stale_edit(self):
        original = self.files.snapshot()["soul"]
        other_device = PersonalFiles(self.store.root)
        saved = other_device.edit("soul", original["revision"], "# Nex\nUse a direct tone.\n")
        self.assertEqual(self.files.snapshot()["soul"], saved)
        self.assertEqual((self.store.root / "context/SOUL.md").read_text(), saved["markdown"])
        with self.assertRaises(PersonalConflict):
            self.files.edit("soul", original["revision"], "Use a playful tone.")

    def test_commands_save_once_and_preferences_are_only_in_authorized_replies(self):
        assistant = Assistant(self.store, None, None)
        command = "Remember that I work on an ocean atlas."
        identifier = str(uuid4())
        assistant.generate_conversation = model([{"name": "save_memory", "query": "I work on an ocean atlas."}])
        token = unlock(self.store)
        for _ in range(2):
            self.assertEqual(chat(assistant, identifier, "mac", command, "en", "UTC", session=token)["engine"], "private_request")
        assistant.generate_conversation = model([{"name": "save_memory", "file": "soul", "query": "Keep your replies practical."}])
        assistant.answer("Update your soul: Keep your replies practical.", "en")
        self.assertEqual(self.files.snapshot()["memory"]["markdown"].count("ocean atlas"), 1)
        assistant.generate_conversation = model([{"name": "memory"}])
        self.assertIn("ocean atlas", assistant.answer("What do you remember about me?", "en")["text"])
        assistant.generate_conversation = model([], compose="A synthetic reply.")
        assistant.answer("What do you think?", "en", spoken=True)
        private = json.loads(assistant.generate_conversation.call_args.args[0][1]["content"])
        self.assertIn("Keep your replies practical.", private["owner_preferences"])
        self.assertNotIn("ocean atlas", json.dumps(private))
        chat(assistant, str(uuid4()), "mac", "Explain plate tectonics", "en", "UTC", session=token)
        public = json.loads(assistant.generate_conversation.call_args.args[0][1]["content"])
        self.assertNotIn("owner_preferences", public)

    def test_memory_tool_prioritizes_matching_evidence_from_the_end_of_the_file(self):
        memory = self.files.read("memory")
        notes = "# Memory\n" + "- Various notes unrelated to the question.\n" * 85 + "- My tea order is jasmine.\n"
        self.files.edit("memory", memory["revision"], notes)
        assistant = Assistant(self.store, None, None)
        assistant.generate_conversation = model([{"name": "memory", "file": "memory", "query": "tea order"}])
        assistant.answer("What kind of tea do I normally order?", "en")
        payload = json.loads(assistant.generate_conversation.call_args.args[0][1]["content"])
        self.assertTrue(payload["tools"]["memory"].startswith("- My tea order is jasmine."))

    def test_now_expires_stale_activity_and_keeps_owner_notes(self):
        initial = self.files.snapshot()["now"]
        content = ("# Now\n\n## Manual\nPrepare the demo.\n\n## Mac screen\n*"
                   + time_key(datetime.now(UTC) - timedelta(hours=1)) + "*\nThe old window.\n")
        self.files.edit("now", initial["revision"], content)
        current = self.files.snapshot()["now"]["markdown"]
        self.assertIn("Prepare the demo.", current)
        self.assertNotIn("The old window.", current)
        latest = datetime.now(UTC)
        self.files.signal("Mac screen", "Screen sharing is paused or stopped.", latest)
        self.files.signal("Mac screen", "A delayed old screenshot caption.", latest - timedelta(seconds=3))
        self.assertNotIn("delayed old", self.files.snapshot()["now"]["markdown"])

    def test_extractor_requires_direct_evidence_for_durable_facts(self):
        events = [
            {"id": "direct", "kind": "dialogue", "created": "now", "text": "I prefer replies in plain English."},
            {"id": "clip", "kind": "ambient", "created": "now", "text": "My name is Chris. We are reviewing the release."},
        ]
        result = {
            "facts": [
                {"source": "direct", "key": "Reply style", "text": "Prefers plain English",
                 "quote": "I prefer replies in plain English"},
                {"source": "clip", "key": "Name", "text": "Chris", "quote": "My name is Chris"},
                {"source": "direct", "key": "City", "text": "Lives on Mars", "quote": "I live on Mars"},
            ],
            "activity": [{"source": "clip", "text": "a release review", "quote": "We are reviewing the release"}],
        }
        self.assertEqual(parse_updates(json.dumps(result), events),
                         ([("Reply style", "Prefers plain English")],
                          [("ambient", "Recorded speech mentions a release review", "now")]))


class PersonalLearningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name, 0, 1_048_576)
        self.files = PersonalFiles(self.store.root)
        memory = self.files.snapshot()["memory"]
        self.files.edit("memory", memory["revision"], "# Memory\n\nKeep this manual note.\n")
        self.statement = "I am working on a solar observatory project."
        with self.store.connect() as db:
            enqueue_context(db, "turn", "dialogue", self.statement, time_key(datetime.now(UTC)))

    async def test_background_process_merges_new_facts_and_drops_work_after_success(self):
        async def generate(messages, _):
            event, = json.loads(messages[1]["content"])["events"]
            evidence = {"source": event["id"], "quote": self.statement}
            return json.dumps({
                "facts": [evidence | {"key": "Project", "text": "Building a solar observatory."}],
                "activity": [evidence | {"text": "Working on a solar observatory."}],
            })

        worker = PersonalLearning(self.store, self.files, generate)
        self.assertTrue(await worker.update())
        snapshot = await asyncio.to_thread(self.files.snapshot)
        self.assertIn("Keep this manual note.", snapshot["memory"]["markdown"])
        self.assertIn("Building a solar observatory.", snapshot["memory"]["markdown"])
        self.assertIn("Working on a solar observatory.", snapshot["now"]["markdown"])
        self.assertFalse(await asyncio.to_thread(worker.pending))

    async def test_temporary_preference_is_not_saved_when_model_returns_no_facts(self):
        with self.store.connect() as db:
            enqueue_context(db, "temporary", "dialogue", "I prefer short replies just for this demo.",
                            time_key(datetime.now(UTC)))

        async def generate(*_):
            return '{"facts":[],"activity":[]}'

        self.assertTrue(await PersonalLearning(self.store, self.files, generate).update())
        self.assertNotIn("short replies", self.files.snapshot()["memory"]["markdown"])
