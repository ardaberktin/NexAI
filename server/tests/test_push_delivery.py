import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from nex.apns import Provider
from nex.goals import Goals
from nex.push_queue import PushQueue
from nex.store import Store
from nex.workspace_content import chat_snapshot


class PushDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), min_free=0, quota=2**30)
        self.queue = PushQueue(self.store)
        self.anchor = datetime.now(UTC)
        self.installation = str(uuid4())
        self.token = "01" * 32
        self.provider = Mock(environment="development")

    def register(self, token=None):
        with patch("nex.push_queue.Provider.load", return_value=self.provider):
            return self.queue.register(self.installation, token or self.token, "development", True, device_id="fixture-iphone")

    def reminder(self):
        goal_id = str(uuid4())
        due = self.anchor + timedelta(seconds=5)
        Goals(self.store).save(goal_id, {"prompt": "Remind me to bring private notes",
                                        "zone": "UTC", "scheduled_at": due.isoformat()},
                               anchor=self.anchor)
        return due

    def run_goal(self, due):
        generate = Mock(side_effect=['{"tools":[]}', "Bring private notes."])
        Goals(self.store, generate=generate).run(due)

    def pending(self):
        with self.store.connect() as db:
            return db.execute("SELECT * FROM goal_push_delivery").fetchall()

    def test_due_reminder_updates_chat_cursor_and_delivers_once_after_a_retry(self):
        self.assertEqual(self.register(), {"active": True})
        before = chat_snapshot(self.store, None)
        self.assertEqual(chat_snapshot(self.store, before["cursor"]), {"cursor": before["cursor"]})
        due = self.reminder()
        self.run_goal(due - timedelta(seconds=1))
        self.assertEqual(self.pending(), [])
        self.run_goal(due)
        after = chat_snapshot(self.store, before["cursor"])
        self.assertNotEqual(before["cursor"], after["cursor"])
        event = after["items"][0]["id"]
        self.assertEqual([row["id"] for row in self.pending()], [event])
        self.provider.send.side_effect = [(503, "Transport"), (200, "")]
        with self.assertLogs(level="WARNING"):
            self.queue.run(due, provider=self.provider)
        self.queue.run(due + timedelta(seconds=2), provider=self.provider)
        self.assertEqual(self.provider.send.call_count, 1)
        self.queue.run(due + timedelta(seconds=6), provider=self.provider)
        self.assertEqual(self.provider.send.call_count, 2)
        self.assertEqual(self.provider.send.call_args.args, (self.token, event, int(due.timestamp()) + 43200))
        self.assertEqual(self.pending(), [])

    def test_registration_requires_a_configured_provider_and_unpair_retires_its_token(self):
        self.assertEqual(self.queue.register(self.installation, self.token, "development", True, device_id="fixture-iphone"), {"active": False})
        with self.assertRaises(ValueError):
            self.queue.register(self.installation, "not a token", "development", True, device_id="fixture-iphone")
        self.register()
        self.run_goal(self.reminder())
        self.queue.register(str(uuid4()), None, None, False, device_id="fixture-iphone")
        self.assertEqual(len(self.pending()), 1)
        self.queue.register(self.installation, None, None, False, device_id="fixture-iphone")
        self.assertEqual(self.pending(), [])

    def test_expired_token_is_removed(self):
        self.register()
        due = self.reminder()
        self.run_goal(due)
        self.provider.send.return_value = (410, "Unregistered")
        with self.assertLogs(level="WARNING"):
            self.queue.run(due, provider=self.provider)
        self.assertEqual(self.pending(), [])
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT token FROM iphone_push_registration").fetchone())

    def test_apple_request_has_generic_payload_and_keeps_bearer_out_of_process_arguments(self):
        provider = Provider("development", "TESTTEAM01", "TESTKEY001", None)
        identifier = str(uuid4())
        with patch.object(provider, "authorization", return_value="provider-secret"), patch(
            "nex.apns.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="\n200"),
        ) as process:
            self.assertEqual(provider.send(self.token, identifier, 9999), (200, ""))
        self.assertNotIn("provider-secret", " ".join(process.call_args.args[0]))
        config = process.call_args.kwargs["input"]
        self.assertIn("api.sandbox.push.apple.com", config)
        self.assertIn(identifier, config)
        payload = next(line.split(" = ", 1)[1] for line in config.splitlines() if line.startswith("data-binary"))
        self.assertEqual(json.loads(json.loads(payload))["aps"]["alert"]["body"], "A goal update is ready.")
