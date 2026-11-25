import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import time
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from uuid import uuid4

from nex.assistant import Assistant
from nex.clock_alerts import ClockAlerts
from nex.conversation.plan import Plan
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goals import Goals
from nex.store import Store, now
from nex.workspace_chat_pages import PAGE_SIZE
from nex.workspace_content import chat_snapshot
from nex.workspace_ideas import WorkspaceIdeas
from nex.workspace_sync import RecordingPresence
from test_hosted_accounts import HostedAccountFixture

CHAT = "/v1/workspace/chat"
RECORDER = "/v1/workspace/iphone-recording"
RECORDING_STATUS = "/v2/workspace/recording-status"
FEATURE_SETTINGS = "/v2/workspace/features"
REPLY = {"text": "Ready.", "language": "en", "sources": [], "engine": "local_model"}


class WorkspaceSyncTests(HostedAccountFixture):
    def setUp(self):
        super().setUp()
        self.store = self.host.owner.store
        self.assistant = self.host.owner.assistant = Assistant(self.store, None, None)
        self.devices = {role: self.register_device(role) for role in ("mac", "iphone")}
        self.sessions = {role: self.unlock(login) for role, login in self.devices.items()}

    def request(self, device, path, body=None, method="GET", status=200, *, workspace=True):
        login = self.devices.get(device, {})
        return super().request(method, path, login.get("token"), body=body, status=status,
                               workspace=self.sessions.get(device) if workspace else None)

    def test_feature_switches_are_shared_and_keep_chat_and_files_available(self):
        features = {"daybook": "/v2/daybook?day=2026-11-20", "activities": "/v2/activities?current=1",
                    "feed": "/v2/workspace/feed", "ideas": "/v2/workspace/ideas", "goals": "/v2/workspace/goals"}
        states = dict.fromkeys(features, True)
        self.assertEqual(self.request("iphone", FEATURE_SETTINGS), states)
        self.request("unpaired", FEATURE_SETTINGS, {"feature": "ideas", "enabled": False}, "PUT", status=401)
        for feature in ("chat", "files"):
            self.request("mac", FEATURE_SETTINGS, {"feature": feature, "enabled": False}, "PUT", status=400)
        for feature, path in features.items():
            self.request("iphone", path)
            states[feature] = False
            self.assertEqual(self.request("mac", FEATURE_SETTINGS, {"feature": feature, "enabled": False}, "PUT"),
                             states)
            self.assertIn("is off", self.request("iphone", path, status=403)["error"])
        self.assertEqual(self.request("iphone", FEATURE_SETTINGS), states)
        self.assertEqual(self.request("iphone", CHAT)["items"], [])
        self.assertEqual(self.request("iphone", "/v2/workspace/artifacts"), {"items": []})
        self.request("iphone", FEATURE_SETTINGS, {"feature": "goals", "enabled": True}, "PUT")
        self.assertEqual(self.request("mac", "/v2/workspace/goals"), {"items": []})

    def test_ideas_sync_one_background_generation_and_keep_cards_if_refresh_fails(self):
        path = "/v2/workspace/ideas"
        idea = {"title": "Plan a sketch", "detail": "Start with a thumbnail.",
                "prompt": "Help me sketch the view from my window.", "destination": "chat"}
        workspace = WorkspaceIdeas(self.store)
        self.request("mac", path, status=403, workspace=False)
        self.request("iphone", path, {}, "POST", status=403, workspace=False)
        pending = self.request("iphone", path)
        self.assertEqual(pending, {"items": [], "pending": True, "failed": False})

        def generate(*_):
            # Neither read nor refresh waits for inference or replaces the in-flight job.
            self.assertEqual(self.request("mac", path), pending)
            self.assertEqual(self.request("iphone", path, {}, "POST"), pending)
            return f"```json\n{json.dumps([idea])}\n```"

        model = Mock(side_effect=generate)
        workspace.run(model)
        workspace.run(model)
        model.assert_called_once()
        saved = self.request("mac", path)
        generated, = saved["items"]
        self.assertEqual((generated["prompt"], generated["opens_goal"]), (idea["prompt"], False))
        self.assertFalse(saved["pending"])
        self.assertEqual(self.request("iphone", path), saved)
        reopened = Store(self.store.root, 0, 2**20)
        self.assertEqual(WorkspaceIdeas(reopened).overview(), saved)

        for response in (None, '{"ideas":', '[]'):
            queued = self.request("mac", path, {}, "POST")
            self.assertTrue(queued["pending"])
            self.assertEqual(queued["items"], saved["items"])
            workspace.run(lambda *_, response=response: response)
            expected = ({"items": [], "pending": False, "failed": False} if response == '[]'
                        else saved | {"failed": True})
            self.assertEqual(self.request("iphone", path), expected)

    def test_ideas_cannot_publish_an_inflight_result_after_a_feature_changes(self):
        path = "/v2/workspace/ideas"
        workspace = WorkspaceIdeas(self.store)
        pending = self.request("mac", path)

        def generate(*_):
            self.request("iphone", FEATURE_SETTINGS, {"feature": "feed", "enabled": False}, "PUT")
            self.assertEqual(self.request("iphone", path), pending)
            return '{"ideas":[{"title":"Story","detail":"Use a saved source","prompt":"Find this story","destination":"chat"}]}'

        workspace.run(generate)
        self.assertEqual(self.request("mac", path), pending)
        workspace.run(lambda *_: '{"ideas":[]}')
        self.assertEqual(self.request("mac", path), {"items": [], "pending": False, "failed": False})

    def test_prompt_goal_runs_via_the_workspace_and_posts_to_chat(self):
        identifier = str(uuid4())
        path = "/v2/workspace/goals/" + identifier
        prompt = "Give me one idea for my next sketch."
        draft = {"prompt": prompt, "frequency_minutes": 0, "zone": "America/Vancouver"}
        saved = self.request("iphone", path, draft, "PUT")
        self.assertEqual((saved["prompt"], saved["scheduled_at"]), (prompt, None))
        self.assertTrue(self.request("mac", path)["can_run_now"])
        self.request("mac", path, {"action": "check", "revision": saved["revision"]}, "POST")
        answers = iter(['{"tools":[]}', "Sketch the view from your window."])
        Goals(self.store, generate=lambda *_: next(answers)).run()
        update, = self.request("iphone", CHAT)["items"]
        self.assertEqual((update["role"], update["goal_id"], update["text"]),
                         ("update", identifier, "Sketch the view from your window."))
        alerts = self.request("mac", "/v1/workspace/goal-alerts?updates=1")["items"]
        alert, = alerts
        self.assertEqual((alert["id"], set(alert)), (update["id"], {"id", "sequence"}))

    def test_clock_cards_and_cancellation_require_unlock(self):
        alert = ClockAlerts(self.store).create(str(uuid4()), "alarm", "Private label", "UTC",
                                               lambda now: now + timedelta(minutes=5))
        overview = "/v1/workspace/goals?clocks=1"
        schedule = "/v1/workspace/clock-alerts"
        cancel = f"/v1/workspace/clocks/{alert['id']}/cancel"
        self.request("unpaired", schedule, status=401, workspace=False)
        self.request("mac", overview, status=403, workspace=False)
        self.request("iphone", cancel, {}, "POST", status=403, workspace=False)
        wire, = self.request("mac", schedule, workspace=False)["items"]
        self.assertEqual(set(wire), {"id", "kind", "due_at"})
        self.assertEqual(self.request("iphone", overview)["clock_alerts"], [alert])
        installation = str(uuid4())
        receipt = {"installation": installation, "alarms": [alert["id"]]}
        self.request("unpaired", schedule, receipt, "PUT", status=401, workspace=False)
        self.request("mac", schedule, receipt, "PUT", status=403, workspace=False)
        self.request("iphone", schedule, receipt | {"alarms": ["invalid"]}, "PUT", status=400, workspace=False)
        self.assertEqual(self.request("iphone", schedule, receipt, "PUT", workspace=False), {"ok": True})
        installed, = self.request("iphone", schedule + "?native_installation=" + installation,
                                  workspace=False)["items"]
        self.assertTrue(installed["native_installed"])
        self.assertTrue(self.request("mac", overview)["clock_alerts"][0]["iphone_alarm_scheduled"])
        self.assertEqual(self.request("iphone", cancel, {}, "POST")["status"], "cancelled")
        self.assertEqual(self.request("mac", schedule, workspace=False)["items"], [])

    def test_follow_up_answer_requires_unlock_and_is_shared_with_chat_and_goal(self):
        path = "/v2/workspace/goals/" + str(uuid4())
        due = datetime.now(UTC) + timedelta(minutes=1)
        self.request("iphone", path, {"prompt": "Check whether the repair is done.",
                                      "scheduled_at": due.isoformat(), "zone": "UTC"}, "PUT")
        tool = {"name": "schedule_follow_up", "use_goal_run": True, "question": "Is the repair done?",
                "choices": ["All fixed", "Still waiting"]}
        jobs = GoalFollowUpJobs(self.store, lambda *_: json.dumps(tool))
        jobs.decide()
        jobs.deliver(due + timedelta(seconds=1))
        latest = self.request("mac", CHAT + "?paged=1")
        entry, = latest["items"]
        self.assertEqual(self.request("iphone", path)["follow_up"], entry["follow_up"])
        answer_path = path + "/follow-ups"
        payload = {"follow_up_id": entry["id"], "choice": 1}
        self.request("iphone", answer_path, payload, "POST", status=403, workspace=False)
        self.request("iphone", answer_path, payload | {"choice": True}, "POST", status=400)
        receipt = self.request("iphone", answer_path, payload, "POST")
        self.assertEqual(receipt["selected_choice"], 1)
        self.assertEqual(self.request("iphone", path)["follow_up"], receipt)
        changed = self.request("mac", CHAT + "?paged=1&cursor=" + latest["cursor"])
        self.assertEqual(changed["items"][0]["follow_up"], receipt)

    def test_delete_goal_removes_its_data_and_keeps_alert_cursors_monotonic(self):
        base = "/v2/workspace/goals"
        alerts = "/v1/workspace/goal-alerts"
        path = base + "/" + str(uuid4())
        other_path = base + "/" + str(uuid4())
        draft = {"prompt": "Give me a writing exercise", "zone": "UTC"}
        other = self.request("mac", other_path, draft | {"prompt": "Give me a sketch idea"}, "PUT")
        goal = self.request("iphone", path, draft, "PUT")
        goal = self.request("mac", path, {"action": "check", "revision": goal["revision"]}, "POST")
        with self.store.connect() as db:
            db.execute("INSERT INTO iphone_push_registration VALUES(1,'install','token','development')")
            db.execute("INSERT INTO goal_seen VALUES(?,'seen-item',?)", (goal["id"], now()))
            db.execute("INSERT INTO goal_mail_review VALUES(?,'reviewed-item',?)", (goal["id"], now()))
        answers = iter(['{"tools":[]}', "Write about a sound.", '{"tools":[]}', "Sketch a window."])
        runner = Goals(self.store, generate=lambda *_: next(answers))
        runner.run()
        alert, = self.request("mac", alerts)["items"]
        update, = self.request("iphone", CHAT + "?paged=1")["items"]
        self.assertEqual(update["id"], alert["id"])
        self.assertEqual(len(self.request("mac", path + "/artifacts")["items"]), 1)

        delete = {"action": "delete", "revision": goal["revision"]}
        self.request("mac", path, delete, "POST", status=403, workspace=False)
        self.request("mac", path, delete | {"revision": goal["revision"] - 1}, "POST", status=409)
        self.assertEqual(self.request("mac", path, delete, "POST"), {"deleted": True})
        self.assertEqual(self.request("iphone", path, delete, "POST"), {"deleted": True})
        self.request("iphone", path, status=404)
        self.request("iphone", path + "/artifacts", status=404)
        self.request("iphone", path, draft, "PUT", status=404)
        self.assertEqual(self.request("iphone", base)["items"], [other])
        self.assertEqual(self.request("iphone", CHAT + "?paged=1")["items"], [])
        self.assertEqual(self.request("iphone", "/v2/workspace/artifacts")["items"], [])
        self.assertEqual(self.request("mac", alerts)["items"], [])
        with self.store.connect() as db:
            for table in ("goal_activity", "goal_seen", "goal_mail_review"):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE goal_id=?", (goal["id"],)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM goal_push_delivery WHERE id=?", (alert["id"],)).fetchone())

        self.request("mac", other_path, {"action": "check", "revision": other["revision"]}, "POST")
        runner.run()
        later_alert, = self.request("mac", alerts + "?" + urlencode({"after": alert["sequence"]}))["items"]
        self.assertGreater(later_alert["sequence"], alert["sequence"])

    def test_question_and_answer_reach_a_waiting_peer_in_both_directions(self):
        for sender, peer in (("iphone", "mac"), ("mac", "iphone")):
            with self.subTest(sender=sender):
                self.check_exchange(sender, peer)

    def check_exchange(self, sender, peer):
        identifier = str(uuid4())
        question = {"id": identifier, "text": "Are you ready?", "language": "en"}
        cursor = self.request(peer, CHAT)["cursor"]
        waiting, finish = Event(), Event()

        def snapshot(store, cursor):
            result = chat_snapshot(store, cursor)
            if "items" not in result:
                waiting.set()
            return result

        def answer(*args, **kwargs):
            if not finish.wait(4):
                raise AssertionError("The peer did not receive the question during generation.")
            return REPLY

        with (patch("nex.workspace_sync.chat_snapshot", side_effect=snapshot),
              patch("nex.privacy.public.select", return_value=Plan(())),
              patch("nex.privacy.public.execute", side_effect=answer) as generate,
              ThreadPoolExecutor(max_workers=2) as clients):
            def watch(version):
                waiting.clear()
                response = clients.submit(self.request, peer, f"{CHAT}?wait=1&cursor={version}")
                self.assertTrue(waiting.wait(2))
                return response

            first = watch(cursor)
            sending = clients.submit(self.request, sender, "/v1/chat", question, "POST")
            try:
                prompt = first.result(timeout=2)
                self.assertEqual(prompt["items"][-1]["id"], "q-" + identifier)
                self.assertFalse(sending.done())
                second = watch(prompt["cursor"])
            finally:
                finish.set()
            self.assertEqual(sending.result(), REPLY)
            complete = second.result(timeout=2)
            self.assertEqual([m["id"] for m in complete["items"] if m["id"].endswith(identifier)],
                             ["q-" + identifier, "a-" + identifier])
            self.assertEqual(self.request(sender, "/v1/chat", question, "POST"), REPLY)
            generate.assert_called_once()
            self.assertEqual(self.request(peer, CHAT + "?cursor=" + complete["cursor"]),
                             {"cursor": complete["cursor"]})

    def test_mac_receives_transitions_and_expiry_from_authenticated_iphone(self):
        self.request("unpaired", RECORDER, status=401)
        self.request("mac", RECORDER, {"recording": True}, "PUT", status=403)
        self.request("iphone", RECORDER, {"recording": "true"}, "PUT", status=400)
        self.assertEqual(self.request("mac", RECORDER)["state"], "unknown")
        self.request("iphone", RECORDER, {"recording": False}, "PUT")
        self.assertEqual(self.request("mac", RECORDER)["state"], "idle")
        with ThreadPoolExecutor(max_workers=1) as client:
            started = client.submit(self.request, "mac", RECORDER + "?previous=idle")
            self.request("iphone", RECORDER, {"recording": True}, "PUT")
            self.assertEqual(started.result(timeout=2)["state"], "recording")
            stopped = client.submit(self.request, "mac", RECORDER + "?previous=recording")
            self.request("iphone", RECORDER, {"recording": False}, "PUT")
            self.assertEqual(stopped.result(timeout=2)["state"], "idle")
        with patch("nex.workspace_sync.RECORDING_LEASE", 0.15):
            self.request("iphone", RECORDER, {"recording": True}, "PUT")
            expired = self.request("mac", RECORDER + "?previous=recording")
            self.assertEqual(expired, {"state": "unknown", "valid_for": 0})

    def test_recording_status_shares_independent_microphones_and_expires_watch_reports(self):
        def report(device, recording, **fields):
            return self.request(device, RECORDING_STATUS, {"recording": recording, **fields},
                                "PUT", workspace=False)

        initial = self.request("mac", RECORDING_STATUS)
        self.assertEqual(set(initial["recorders"]), {"mac", "iphone", "watch"})
        self.assertTrue(all(item["state"] == "unknown" for item in initial["recorders"].values()))
        report("iphone", False)
        report("mac", True)
        idle = self.request("mac", RECORDING_STATUS)
        waiting_path = RECORDING_STATUS + "?" + urlencode({"previous": idle["cursor"]})
        changed = self.host.owner.recording_status.changed
        wait = changed.wait
        both_waiting = Event()
        reader_count = 0

        def wait_for_report(timeout):
            nonlocal reader_count
            reader_count += 1
            if reader_count == 2:
                both_waiting.set()
            return wait(timeout)

        with (patch.object(changed, "wait", side_effect=wait_for_report),
              ThreadPoolExecutor(max_workers=2) as clients):
            readers = [clients.submit(self.request, device, waiting_path) for device in ("mac", "iphone")]
            self.assertTrue(both_waiting.wait(2))
            report("iphone", True, source="watch", valid_for=20)
            for reader in readers:
                states = reader.result(timeout=2)["recorders"]
                self.assertEqual({name: item["state"] for name, item in states.items()},
                                 {"mac": "recording", "iphone": "idle", "watch": "recording"})
                self.assertGreater(states["watch"]["valid_for"], 0)
                self.assertLessEqual(states["watch"]["valid_for"], 20)

        report("iphone", False, source="watch", valid_for=20)
        self.assertEqual(self.request("iphone", RECORDING_STATUS)["recorders"]["watch"]["state"], "idle")
        report("iphone", True, source="watch", valid_for=0.2)
        active = self.request("mac", RECORDING_STATUS)
        waiting_path = RECORDING_STATUS + "?" + urlencode({"previous": active["cursor"]})
        expired = self.request("mac", waiting_path)["recorders"]
        self.assertEqual(expired["watch"], {"state": "unknown", "valid_for": 0})
        self.assertEqual(expired["mac"]["state"], "recording")
        self.assertEqual(self.request("mac", RECORDER)["state"], "idle")

    def test_sleeping_watch_keeps_recording_until_a_newer_stop(self):
        start = time() - 7200
        def transition(recording, changed_at, valid_for=30):
            self.request("iphone", RECORDING_STATUS, {"source": "watch", "recording": recording,
                         "valid_for": valid_for, "changed_at": changed_at}, "PUT", workspace=False)

        def watch():
            return self.request("mac", RECORDING_STATUS)["recorders"]["watch"]

        transition(True, start)
        with patch("nex.workspace_sync.monotonic", return_value=10**12):
            self.assertEqual(watch(), {"state": "recording", "until_changed": True, "valid_for": 30})
        self.host.owner.recording_status = RecordingPresence(self.store)
        self.assertTrue(watch()["until_changed"])
        transition(False, start + 120)
        transition(True, start)
        self.assertEqual(watch()["state"], "idle")
        self.host.owner.recording_status = RecordingPresence(self.store)
        transition(True, start)
        self.assertEqual(watch()["state"], "unknown")
        transition(True, start + 180)
        self.assertTrue(watch()["until_changed"])
        transition(False, start + 181, valid_for=0)
        self.assertEqual(watch()["state"], "unknown")

    def test_recording_status_preserves_workspace_auth_and_validates_watch_relay(self):
        self.request("unpaired", RECORDING_STATUS, {"recording": True}, "PUT", status=401, workspace=False)
        self.request("mac", RECORDING_STATUS, status=403, workspace=False)
        for device, source in (("mac", "watch"), ("mac", "iphone"), ("iphone", "mac")):
            self.request(device, RECORDING_STATUS, {"recording": True, "source": source}, "PUT", status=403)
        for lease in (-1, "20", True, None):
            self.request("iphone", RECORDING_STATUS, {"recording": True, "source": "watch", "valid_for": lease},
                         "PUT", status=400)
        for stamp in (-1, True, "yesterday", time() + 3600):
            self.request("iphone", RECORDING_STATUS, {"recording": True, "source": "watch", "valid_for": 30,
                         "changed_at": stamp}, "PUT", status=400)

    def test_older_push_to_talk_recordings_sync_once_with_their_original_ids(self):
        identifier, started = str(uuid4()), now()
        self.store.save_recording(identifier, "iphone", started, 2, "voice", False, "en", b"audio")
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state='ready',reply=? WHERE id=?", (json.dumps(REPLY), identifier))
            self.store.add_memory(db, identifier, started, "recording", "iphone", "Are you ready?")
            self.store.add_memory(db, "a-" + identifier, started, "answer", "iphone", "Ready.", [identifier])
        messages = self.request("mac", CHAT)["items"]
        self.assertEqual([(m["id"], m["text"], m["sources"]) for m in messages],
                         [("q-" + identifier, "Are you ready?", []), ("a-" + identifier, "Ready.", [])])
        self.assertEqual(self.request("mac", CHAT + "?paged=1")["items"], messages)
        with self.store.connect() as db:
            self.store.add_memory(db, "q-" + identifier, started, "question", "iphone", "Are you ready?")
        self.assertEqual(self.request("mac", CHAT)["items"], messages)
        self.assertEqual(self.request("mac", CHAT + "?paged=1")["items"], messages)

    def test_private_recording_handoffs_do_not_reappear_as_ordinary_chat_questions(self):
        identifier, started = str(uuid4()), now()
        self.store.save_recording(identifier, "iphone", started, 2, "voice", False, "en", b"audio")
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state='ready',reply=? WHERE id=?",
                       (json.dumps(REPLY | {"engine": "private_request"}), identifier))
            self.store.add_memory(db, identifier, started, "recording", "iphone", "A private question")
        self.assertEqual(self.request("mac", CHAT)["items"], [])
        with self.store.connect() as db:
            self.store.add_memory(db, "q-" + identifier, started, "question", "iphone", "A private question")
            self.store.add_memory(db, "a-" + identifier, started, "answer", "iphone", "Approve on iPhone",
                                  [], "private_request")
        self.assertEqual(self.request("iphone", CHAT)["items"], [])
        self.assertEqual(self.request("iphone", CHAT + "?paged=1")["items"], [])

    def test_history_pages_cover_the_archive_despite_tied_dates_and_a_new_message(self):
        same_time, early, late = "2026-11-20T08:00:00.000+00:00", "2026-11-19T08:00:00.000+00:00", now()
        expected = []
        with self.store.connect() as db:
            self.store.add_memory(db, "q-first", early, "question", "mac", "First")
            for number in range(PAGE_SIZE + 1):
                identifier = f"{number:03}"
                self.store.add_memory(db, "q-" + identifier, same_time, "question", "mac", "Question")
                self.store.add_memory(db, "a-" + identifier, early, "answer", "iphone", "Answer", [identifier])
                expected.append(identifier)
            db.execute("""INSERT INTO goals(id,title,prompt,zone,interval_minutes,status,created,updated)
                          VALUES('goal','Goal','Prompt','UTC',0,'active',?,?)""",
                       (early, early))
            db.execute("INSERT INTO goal_activity(id,goal_id,created,title,detail,artifact) "
                       "VALUES('update','goal',?,'Update','Details',1)", (same_time,))
        latest = self.request("mac", CHAT + "?paged=1")
        self.assertEqual(len(latest["items"]), PAGE_SIZE)
        self.assertEqual(self.request("iphone", CHAT + "?" + urlencode({"paged": "1", "cursor": latest["cursor"]})),
                         {"cursor": latest["cursor"]})
        with self.store.connect() as db:
            self.store.add_memory(db, "q-new", late, "question", "mac", "New")
        watched = self.request("iphone", CHAT + "?" + urlencode({"paged": "1", "wait": "1", "cursor": latest["cursor"]}))
        self.assertTrue(watched["watch"])
        self.assertEqual(watched["items"][-1]["id"], "q-new")

        items = latest["items"]
        before = latest["next_before"]
        while before:
            page = self.request("iphone", CHAT + "?" + urlencode({"before": before}))
            self.assertLessEqual(len(page["items"]), PAGE_SIZE)
            self.assertNotEqual(page["next_before"], before)
            items = page["items"] + items
            before = page["next_before"]
        self.assertEqual([m["id"] for m in items], ["q-first"] + ["q-" + value for value in expected]
                         + ["a-" + value for value in expected] + ["update"])
        self.assertTrue(all(m["date"] == same_time and not m["sources"] for m in items[1:]))
        self.assertEqual(items[-1]["title"], "Goal · Update")
        self.request("mac", CHAT + "?before=invalid", status=400)
        self.request("unpaired", CHAT + "?" + urlencode({"before": latest["next_before"]}), status=401)
