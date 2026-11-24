import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

from conversation_fixtures import model, unlock
from nex.clock_alerts import ClockAlerts
from nex.conversation.clock_tool import CHOOSE_CANCELLATION, timing
from nex.conversation.sync import make_turn
from nex.live.public_reply import public_reply
from nex.privacy import public, requests
from nex.push_queue import PushQueue
from nex.store import Store

ZONE = "America/Vancouver"
TIMER = {"name": "clock", "operation": "create", "kind": "timer", "duration_seconds": 7200}


class ClockAlertTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self.enterContext(tempfile.TemporaryDirectory()), 0, 2**20)
        self.alerts = ClockAlerts(self.store)
        self.assistant = Mock(store=self.store, settings=None)
        self.session = unlock(self.store)

    def test_chat_uses_model_timing_and_returns_the_saved_cards_once(self):
        anchor = datetime.now(UTC) + timedelta(days=1)
        local = anchor.astimezone(ZoneInfo(ZONE)).replace(hour=20)
        clock = {"unix_seconds": int(local.timestamp()), "time_zone": ZONE,
                 "utc_offset_seconds": int(local.utcoffset().total_seconds())}
        cases = (
            ("Give me a two-hour countdown", TIMER),
            ("Nex, set an alarm for 10pm", {"name": "clock", "operation": "create", "kind": "alarm", "local_time": "22:00"}),
            ("Set a timer for 10pm", {"name": "clock", "operation": "create", "kind": "timer", "local_time": "22:00"}),
        )
        for (question, call), intent in zip(cases, ("CLOCK", "SCHEDULE", "OTHER"), strict=True):
            with self.subTest(question=question):
                identifier = str(uuid4())

                def plan(data, question=question, call=call):
                    self.assertEqual(data["question"], question)
                    self.assertEqual(data.get("local_time_zone", data.get("goal_time_zone")), ZONE)
                    return [call]

                def compose(data, identifier=identifier):
                    receipt = data["tools"]["clock"]["alert"]
                    self.assertEqual(receipt, self.alerts.for_requests([identifier])[identifier] |
                                     {"local_deadline": receipt["local_deadline"]})
                    return f"Saved as {receipt['kind']} with deadline {receipt['local_deadline']}."

                self.assistant.generate_conversation = model(plan, compose=compose, goal_intent=intent)
                for _ in range(2):
                    reply = public.chat(self.assistant, identifier, "mac", question, "en", ZONE,
                                        session=self.session, device_clock=clock)
                    self.assertEqual((reply["engine"], reply["text"]), ("private_request", ""))
                item = next(row for row in requests.inbox(self.store.privacy, self.store)["items"]
                            if row["id"] == identifier)
                alert = item["clock_alert"]
                due = datetime.fromisoformat(alert["due_at"])
                self.assertEqual(alert["kind"], call["kind"])
                if "duration_seconds" in call:
                    self.assertEqual(due - datetime.fromisoformat(alert["created_at"]), timedelta(hours=2))
                else:
                    self.assertEqual(due, local.replace(hour=22, minute=0, second=0, microsecond=0))
                self.assertIn(alert, self.alerts.overview())
                self.assertIn("Saved as", item["answer"])
        self.assertEqual(len(self.alerts.overview()), 3)

    def test_voice_paths_save_only_after_existing_workspace_approval(self):
        self.assistant.generate_conversation = model([TIMER], compose="Model-generated timer receipt.")
        complete = AsyncMock(side_effect=lambda _, messages, _tokens, **options:
                             self.assistant.generate_conversation(messages, **options))
        dialogue = Mock(memory=self.store, complete=complete)
        emit = Mock()
        voice = SimpleNamespace(id=str(uuid4()), device="iphone", text="Start a countdown for two hours",
                                language="en", zone=ZONE, started=datetime.now(UTC), journal=None,
                                speaker="recognized")
        live = asyncio.run(public_reply(dialogue, voice, (), emit))
        emit.assert_not_called()
        self.assertEqual(live["text"], "")
        identifier = str(uuid4())
        recorded = public.answer(self.assistant, self.store.privacy, identifier, "watch", "Two-hour timer please",
                                 "en", ZONE, spoken=True, asked_at=voice.started.isoformat(), emit=emit)
        self.assertEqual(recorded["text"], "")
        emit.assert_not_called()
        self.assertEqual(self.alerts.overview(), [])
        for pending in (voice.id, identifier):
            requests.resolve(self.store.privacy, "mac", self.session, pending, self.assistant, None)
        self.assertEqual(len(self.alerts.overview()), 2)

    def test_model_can_decline_the_tool_or_use_its_error(self):
        self.assistant.generate_conversation = model([], compose="A timer would count down to zero.")
        public.chat(self.assistant, str(uuid4()), "mac", "How would a timer for 10pm work?", "en", ZONE,
                    session=self.session)
        self.assertEqual(self.alerts.overview(), [])

        def compose(data):
            self.assertEqual(data["tools"]["clock"]["error"], "invalid_clock_arguments")
            return "Which time should the alarm use?"

        self.assistant.generate_conversation = model([{"name": "clock", "operation": "create", "kind": "alarm"}],
                                                     compose=compose)
        public.chat(self.assistant, str(uuid4()), "mac", "I need an alarm", "en", ZONE, session=self.session)
        item = requests.inbox(self.store.privacy, self.store)["items"][0]
        self.assertEqual(item["answer"], "Which time should the alarm use?")
        self.assertNotIn("clock_alert", item)

    def test_model_cancellation_and_durable_delivery(self):
        now = datetime.now(UTC)
        first, second = (self.alerts.create(str(uuid4()), "timer", label, ZONE,
                                          lambda start: start + timedelta(seconds=10), anchor=now)
                         for label in ("Tea", "Laundry"))
        decision = model([{"name": "clock", "operation": "cancel"}], compose="The tea timer is cancelled.")

        def generate(messages, *args, **kwargs):
            if messages[0]["content"] == CHOOSE_CANCELLATION:
                self.assertEqual({row["label"] for row in json.loads(messages[1]["content"])["active_alerts"]},
                                 {"Tea", "Laundry"})
                return json.dumps({"id": first["id"]})
            return decision(messages, *args, **kwargs)

        self.assistant.generate_conversation = generate
        public.chat(self.assistant, str(uuid4()), "mac", "Stop the tea one", "en", ZONE, session=self.session)
        token = "ab" * 32
        with self.store.connect() as db:
            db.execute("INSERT INTO iphone_push_registration VALUES(1,?,?,'development')", (str(uuid4()), token))
        self.assertEqual(self.alerts.notification_schedule("iphone")["items"], [])
        self.assertEqual(self.alerts.notification_schedule("mac")["items"],
                         [{key: second[key] for key in ("id", "kind", "due_at")}])
        due = datetime.fromisoformat(second["due_at"])
        self.alerts.run(due - timedelta(seconds=1))
        provider = Mock(environment="development")
        provider.send.return_value = (200, "")
        push = PushQueue(self.store)
        push.run(due, provider=provider)
        provider.send.assert_not_called()
        for _ in range(2):
            self.alerts.run(due)
            push.run(due, provider=provider)
        provider.send.assert_called_once_with(token, second["id"], int(due.timestamp()) + 15, clock_kind="timer")
        self.assertEqual({row["id"]: row["status"] for row in self.alerts.overview()},
                         {first["id"]: "cancelled", second["id"]: "fired"})

    def test_native_alarm_confirmation_suppresses_only_matching_iphone_push(self):
        installation, previous = str(uuid4()), str(uuid4())
        now = datetime.now(UTC)
        due = now + timedelta(minutes=5)
        pinned, unconfirmed, revoked, replaced, timer, cancelled = [
            self.alerts.create(str(uuid4()), kind, "Private label", ZONE, lambda _: due, anchor=now)
            for kind in ("alarm", "alarm", "alarm", "alarm", "timer", "alarm")
        ]
        with self.store.connect() as db:
            db.execute("INSERT INTO iphone_push_registration VALUES(1,?,?,'development')", (installation, "ab" * 32))
        options = {"installation": installation, "anchor": now}
        snapshot = self.alerts.notification_schedule("iphone", **options)
        self.assertTrue(snapshot["uses_push"])
        self.assertEqual({row["kind"] for row in snapshot["items"]}, {"alarm"})
        self.assertTrue(all(set(row) == {"id", "kind", "due_at", "native_installed"} for row in snapshot["items"]))
        self.assertEqual(self.alerts.notification_schedule("iphone"), {"items": []})

        self.alerts.report_native(installation, [pinned["id"], revoked["id"], timer["id"], cancelled["id"]])
        self.alerts.report_native(previous, [replaced["id"]])
        self.alerts.report_native(installation, [pinned["id"], cancelled["id"]])
        self.alerts.cancel(cancelled["id"])
        installed = {row["id"] for row in self.alerts.notification_schedule("iphone", **options)["items"]
                     if row["native_installed"]}
        self.assertEqual(installed, {pinned["id"]})
        cards = {row["id"]: row for row in self.alerts.overview()}
        self.assertTrue(cards[pinned["id"]]["iphone_alarm_scheduled"])
        self.assertNotIn("iphone_alarm_scheduled", cards[replaced["id"]])
        self.assertIn(pinned["id"], {row["id"] for row in self.alerts.notification_schedule("mac")["items"]})

        self.alerts.run(due)
        with self.store.connect() as db:
            pushed = {row["id"] for row in db.execute("SELECT id FROM goal_push_delivery")}
        self.assertEqual(pushed, {alert["id"] for alert in (unconfirmed, revoked, replaced, timer)})

    def test_saved_clock_stays_visible_when_model_cannot_write_a_reply(self):
        self.assistant.generate_conversation = model([TIMER], compose=lambda _: "")
        identifier = str(uuid4())
        reply = public.chat(self.assistant, identifier, "mac", "Start a two-hour timer", "en", ZONE,
                            session=self.session)
        item, = requests.inbox(self.store.privacy, self.store)["items"]
        self.assertEqual((item["state"], item["answer"], reply["text"]), ("failed", None, ""))
        self.assertIn(item["clock_alert"], self.alerts.overview())

    def test_clock_time_rolls_forward_and_handles_daylight_saving(self):
        zone = ZoneInfo(ZONE)
        anchor = datetime(2026, 11, 23, 23, 30, tzinfo=zone)
        turn = make_turn(str(uuid4()), "mac", "", "en", ZONE, asked_at=anchor)
        self.assertEqual(timing({"local_time": "22:00"}, turn, zone)(anchor).astimezone(zone).isoformat(),
                         datetime(2026, 11, 24, 22, tzinfo=zone).isoformat())
        zone = ZoneInfo("America/Los_Angeles")
        turn.started = datetime(2026, 11, 1, 8, 45, tzinfo=UTC)
        self.assertEqual(timing({"local_time": "01:30"}, turn, zone)(turn.started),
                         datetime(2026, 11, 1, 9, 30, tzinfo=UTC))
        with self.assertRaises(ValueError):
            timing({"local_at": "2027-03-14T02:30"}, turn, zone)
