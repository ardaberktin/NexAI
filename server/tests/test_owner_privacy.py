import base64
import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from threading import Barrier, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from conversation_fixtures import model
from nex.conversation_history import watch_dialogue
from nex.http_api import Server, recorder_operation, recorder_receipt
from nex.privacy import keys, public, requests
from nex.privacy.chat_history import ChatFollowups
from nex.privacy.policy import request_kind
from nex.privacy.state import AccessDenied
from nex.store import Store, now


class OwnerPrivacyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root, 0, 2**20)
        self.state = self.store.privacy
        self.phone, self.mac = self.root / "phone.pem", self.root / "mac.pem"
        self.sessions = {}
        for device, private in (("iphone", self.phone), ("mac", self.mac)):
            subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(private)],
                           check=True, capture_output=True)
            self.register(device, private)

    def proof(self, device, key, purpose, subject=""):
        challenge = keys.challenge(self.state, device, purpose, subject)
        return self.sign(key, challenge)

    @staticmethod
    def sign(key, challenge):
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=challenge["message"].encode(),
                                   capture_output=True, check=True).stdout
        return {"nonce": challenge["nonce"], "signature": base64.b64encode(signature).decode()}

    def register(self, device, key, *, setup=None):
        public_key = subprocess.run(["openssl", "ec", "-in", str(key), "-pubout", "-outform", "DER"],
                                    capture_output=True, check=True).stdout[-65:]
        proof = self.proof(device, key, "unlock") | {"public": base64.b64encode(public_key).decode(),
                "setup": keys.issue_setup(self.state, device) if setup is None else setup}
        self.sessions[device] = keys.unlock(self.state, device, proof)["token"]

    def pending(self, question="Read my calendar"):
        identifier = str(uuid4())
        requests.queue(self.state, identifier, "mac", question, "en", "UTC", speaker="recognized")
        return requests.inbox(self.state)["items"][0]

    def test_phone_approval_is_scoped_one_use_and_display_only(self):
        item = self.pending()
        assistant = Mock()
        complete = self.enterContext(patch("nex.privacy.requests.approved_reply", return_value={"text": "Sample private appointment"}))
        with self.assertRaises(AccessDenied):
            self.proof("mac", self.mac, "approve", item["subject"])
        wrong = self.proof("iphone", self.mac, "approve", item["subject"])
        with self.assertRaises(AccessDenied):
            requests.approve(self.state, "iphone", item["id"], wrong, assistant)
        assistant.answer.assert_not_called()
        proof = self.proof("iphone", self.phone, "approve", item["subject"])
        barrier = Barrier(2)

        def approve():
            barrier.wait(timeout=5)
            try:
                requests.approve(self.state, "iphone", item["id"], proof, assistant)
                return True
            except AccessDenied:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(f.result() for f in [pool.submit(approve), pool.submit(approve)]), [False, True])
        complete.assert_called_once()
        self.assertEqual(requests.inbox(self.state)["items"][0]["answer"], "Sample private appointment")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory").fetchone()[0], 0)

    def test_expired_replaced_and_cross_device_credentials(self):
        item = self.pending()
        proof = self.proof("iphone", self.phone, "approve", item["subject"])
        with self.state.transaction(write=True) as db:
            db.execute("UPDATE challenges SET expires=0 WHERE id=?", (proof["nonce"],))
        with self.assertRaises(AccessDenied):
            requests.approve(self.state, "iphone", item["id"], proof, Mock())
        proof = self.proof("iphone", self.phone, "approve", item["subject"])
        with self.state.transaction(write=True) as db:
            db.execute("DELETE FROM requests WHERE id=?", (item["id"],))
        requests.queue(self.state, item["id"], "mac", "Read my mail", "en", "UTC")
        with self.assertRaises(AccessDenied):
            requests.approve(self.state, "iphone", item["id"], proof, Mock())
        with self.assertRaises(AccessDenied):
            self.register("iphone", self.mac, setup="unissued")
        with self.assertRaises(AccessDenied):
            keys.require_session(self.state, "mac", self.sessions["iphone"])
        old = self.sessions["iphone"]
        self.register("iphone", self.mac)
        with self.assertRaises(AccessDenied):
            keys.require_session(self.state, "iphone", old)

    def test_public_path_never_reads_owner_context(self):
        assistant = Mock(store=self.store)
        assistant.choose_python.return_value = None
        assistant.generate_conversation = model([], compose="Four.")
        assistant.answer_web.return_value = {"text": "Public web result", "language": "en", "sources": [], "engine": "web_lookup"}
        for text in ["Explain tides.", "How do I replace my bicycle tire?", "What is Gmail?"]:
            result = public.answer(assistant, self.state, str(uuid4()), "mac", text, "en", "UTC", spoken=True)
            self.assertTrue(result["public_voice"])
        assistant.answer.assert_not_called()
        assistant.personal.snapshot.assert_not_called()
        for text in ["What's on my screen?", "What is my name?", "Read my Gmail", "What did I just ask?",
                     "What's the weather during my appointments?", "Remember that my preferred name is Z"]:
            with self.subTest(text=text):
                before = assistant.generate_conversation.call_count
                decisions = assistant.choose_python.call_count
                result = public.answer(assistant, self.state, str(uuid4()), "mac", text, "en", "UTC", spoken=True)
                self.assertEqual(result["engine"], "private_request")
                self.assertEqual(before + 2, assistant.generate_conversation.call_count)
                self.assertEqual(decisions, assistant.choose_python.call_count)
        self.assertEqual(request_kind("Explain gravity", "UTC", force=True), "private")

    def test_public_calculation_followup_is_limited_to_its_unlocked_chat(self):
        cache = ChatFollowups()
        assistant = Mock(store=self.store)
        assistant.answer_account.return_value = {"text": "Private inbox"}
        assistant.answer_python.return_value = {"text": "$14.40", "language": "en", "sources": [],
                                               "engine": "python_sandbox"}
        assistant.generate_conversation = model([], compose="$14.40")
        original = "Dinner was $86.40 for six. What does each person owe?"
        session = self.sessions["mac"]
        public.chat(assistant, str(uuid4()), "mac", original, "en", "UTC", followups=cache, session=session)
        self.assertEqual(cache.previous("iphone", session), ())
        self.assertEqual(cache.previous("mac", "another-session"), ())
        with patch("nex.privacy.chat_history.monotonic", return_value=float("inf")):
            self.assertEqual(cache.previous("mac", session), ())
        followup = "A seventh person just joined. What's each share now?"
        public.chat(assistant, str(uuid4()), "mac", followup, "en", "UTC", followups=cache, session=session)
        history = json.loads(assistant.generate_conversation.call_args.args[0][1]["content"])["history"]
        self.assertEqual([item["content"] for item in history], [original, "$14.40"])
        public.chat(assistant, str(uuid4()), "mac", "Read my inbox", "en", "UTC", followups=cache, session=session)
        self.assertTrue(cache.previous("mac", session)[-1]["private"])

    def test_gmail_followups_stay_private_and_retries_keep_the_original_filter(self):
        cache = ChatFollowups()
        assistant = Mock(store=self.store)
        def choose(data):
            text = data["question"]
            if "unread emails" in text:
                return [{"name": "gmail", "unread": True, "priority": True}]
            if "my emails" in text:
                return [{"name": "gmail", "query": "tuition"}]
            if "the ones" in text and data["history"] and "gmail" in data["history"][-1]["content"]:
                return [{"name": "gmail", "query": "UVic", "unread": True}]
            return []

        assistant.generate_conversation = model(choose, compose="Private Gmail result")
        account = self.enterContext(patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [], "")))
        session = self.sessions["mac"]

        def ask(question, *, identifier=None, device="mac"):
            return public.chat(assistant, identifier or str(uuid4()), device, question, "en", "UTC",
                               followups=cache, session=self.sessions[device])

        self.assertEqual(ask("Look thru my unread emails. Anything important?")["engine"], "private_request")
        self.assertTrue(account.call_args.args[1].priority)
        self.assertFalse(cache.previous("iphone", session))
        self.assertFalse(cache.previous("mac", "another-session"))

        identifier = str(uuid4())
        followup = "Show me the ones about UVic"
        result = ask(followup, identifier=identifier)
        self.assertEqual(result["engine"], "private_request")
        self.assertNotIn("Private Gmail result", result["text"])
        intent = account.call_args.args[1]
        self.assertEqual(intent.topic, "UVic")
        self.assertTrue(intent.unread)
        self.assertFalse(intent.priority)

        ask("No, show me my emails about tuition")
        self.assertEqual(account.call_args.args[1].topic, "tuition")
        self.assertEqual(ask(followup, identifier=identifier), result)
        self.assertEqual(account.call_count, 3)
        assistant.generate_conversation = model(choose, compose="A university.")
        self.assertEqual(ask("What is UVic?")["engine"], "local_model")
        self.assertFalse(cache.previous("mac", session)[-1].get("private"))
        ask(followup)
        self.assertEqual(account.call_count, 3)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM memory WHERE body=?", ("Private Gmail result",)).fetchone())

    def test_recording_receipts_and_background_routes(self):
        identifier = str(uuid4())
        base = "/v1/recordings/" + identifier
        for path in ["/v1/privacy/inbox", "/v1/context", "/v1/memory", "/v1/workspace/chat", base, base + "/audio"]:
            self.assertFalse(recorder_operation("GET", path))
        self.assertTrue(recorder_operation("GET", base + "/receipt"))
        row = {"id": identifier, "started": "", "mode": "voice", "state": "ready", "error": None,
                   "transcript": "private transcript", "notes": "private notes", "reply": {"text": "old answer"}}
        self.assertNotIn("private", json.dumps(recorder_receipt(row)))
        self.assertIsNone(recorder_receipt(row)["reply"])
        with patch("nex.privacy.keys.time", return_value=10**11), self.assertRaises(AccessDenied):
            keys.require_session(self.state, "iphone", self.sessions["iphone"])

    def test_watch_question_retry_and_followup_scope(self):
        room = str(uuid4())
        previous = str(uuid4())
        stamp = "2026-11-22T12:00:00.000+00:00"
        args = (previous, "watch", stamp, 2.5, "voice", False, "en", b"sample")
        self.assertTrue(self.store.save_recording(*args, watch_conversation=room)[1])
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET state='ready', transcript='Hello', reply=? WHERE id=?",
                       (json.dumps({"text": "Hello there", "engine": "local_model", "public_voice": True}), previous))
        recovered, fresh = self.store.save_recording(*args, watch_conversation=room)
        self.assertFalse(fresh)
        self.assertEqual(recorder_receipt(recovered)["reply"]["text"], "Hello there")
        turn = {"watch_conversation": room, "started": "2026-11-22T12:01:00.000+00:00"}
        self.assertEqual([item["text"] for item in watch_dialogue(self.store, turn)], ["Hello", "Hello there"])
        self.assertEqual(watch_dialogue(self.store, turn | {"watch_conversation": str(uuid4())}), [])
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET reply=? WHERE id=?",
                       (json.dumps({"text": "Review in Nex", "engine": "private_request"}), previous))
        self.assertEqual(watch_dialogue(self.store, turn), [])

    def test_paired_apps_use_the_unlock_session_for_private_chat_and_voice(self):
        assistant = Mock(store=self.store)
        complete = self.enterContext(patch("nex.privacy.requests.approved_reply", return_value={"text": "Sample private event"}))
        assistant.generate_conversation = model([{"name": "calendar"}])
        app = SimpleNamespace(store=self.store, assistant=assistant, chat_slot=Lock(), worker=Mock(),
                              keys={"mac": "mac-test", "iphone": "iphone-test"})
        server = Server(("127.0.0.1", 0), app)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        clip = str(uuid4())
        audio = b"\x00\x00\x00\x20ftyp" + b"\x00" * 24
        headers = {"Content-Type": "audio/mp4", "X-Nex-Started": now(),
                   "X-Nex-Duration": "1.000", "X-Nex-Mode": "ambient"}
        for device, status in (("mac", 403), ("iphone", 201), ("iphone", 200)):
            with self.subTest(watch_upload=device, expected=status):
                connection = HTTPConnection(*server.server_address, timeout=4)
                try:
                    connection.request("PUT", "/v2/watch/recordings/" + clip, audio,
                                       headers | {"Authorization": "Bearer " + app.keys[device]})
                    result = connection.getresponse()
                    receipt = json.load(result)
                    self.assertEqual(result.status, status, receipt)
                    if status < 300:
                        self.assertEqual(receipt["id"], clip)
                        self.assertNotIn("transcript", receipt)
                finally:
                    connection.close()
        self.assertEqual(self.store.recording(clip)["device"], "watch")
        def send(device, path, *, body=None, locked=False, status=200):
            headers = {"Authorization": "Bearer " + app.keys[device], "Content-Type": "application/json"}
            if not locked:
                headers["X-Nex-Workspace"] = self.sessions[device]
            connection = HTTPConnection(*server.server_address, timeout=4)
            try:
                connection.request("POST" if body is not None else "GET", "/v2/" + path,
                                   json.dumps(body) if body is not None else None, headers)
                response = connection.getresponse()
                data = json.load(response)
                self.assertEqual(response.status, status, data)
                return data
            finally:
                connection.close()

        receipt_path = "watch/recordings/" + clip + "/receipt"
        send("mac", receipt_path, locked=True, status=403)
        receipt = send("iphone", receipt_path, locked=True)
        self.assertEqual(receipt["id"], clip)
        self.assertNotIn("transcript", receipt)
        with self.store.connect() as db:
            db.execute("UPDATE recordings SET reply=? WHERE id=?",
                       (json.dumps({"text": "private reply", "engine": "archive"}), clip))
        self.assertIsNone(send("iphone", receipt_path, locked=True)["reply"])
        phone_clip = str(uuid4())
        self.store.save_recording(phone_clip, "iphone", now(), 1, "voice", False, "en", audio)
        send("iphone", "watch/recordings/" + phone_clip + "/receipt", locked=True, status=404)

        question = {"id": str(uuid4()), "text": "Read my calendar", "language": "en"}
        send("iphone", "recordings/" + clip, locked=True, status=403)
        self.assertEqual(send("mac", "recordings/" + clip)["device"], "watch")
        send("mac", "chat", body=question, locked=True, status=403)
        self.assertIsNone(send("mac", "web/chat", body=question))
        assistant.answer.assert_not_called()
        reply = send("mac", "chat", body=question)
        self.assertEqual(reply["engine"], "private_request")
        self.assertNotIn("Sample private event", reply["text"])
        self.assertEqual(send("mac", "chat", body=question), reply)
        complete.assert_called_once()
        for peer in ["iphone", "mac"]:
            item, = send(peer, "privacy/inbox")["items"]
            self.assertEqual(item["answer"], "Sample private event")
            self.assertNotIn("Sample private event", json.dumps(send(peer, "workspace/chat")))

        voice = str(uuid4())
        requests.queue(self.state, voice, "iphone", "Read my calendar", "en", "UTC", speaker="recognized")
        send("mac", "privacy/resolve", body={"id": voice}, locked=True, status=403)
        for peer in ["iphone", "mac"]:
            send(peer, "privacy/inbox", locked=True, status=403)
            pending = next(row for row in send(peer, "privacy/inbox")["items"] if row["id"] == voice)
            self.assertIsNone(pending["answer"])
        send("mac", "privacy/resolve", body={"id": voice})
        send("iphone", "privacy/resolve", body={"id": voice})
        for peer in ["iphone", "mac"]:
            ready = next(row for row in send(peer, "privacy/inbox")["items"] if row["id"] == voice)
            self.assertEqual(ready["answer"], "Sample private event")
            self.assertEqual(ready["asked_at"], pending["asked_at"])
            self.assertGreaterEqual(ready["updated_at"], ready["asked_at"])
            self.assertNotIn("Sample private event", json.dumps(send(peer, "workspace/chat")))
        send("mac", "privacy/lock", body={})
        send("mac", "privacy/inbox", status=403)
        send("mac", "privacy/resolve", body={"id": voice}, status=403)
        send("mac", "chat", body=question, status=403)
        self.assertEqual(complete.call_count, 2)

    def test_screen_capture_and_explicitly_private_weather_wait_for_phone_approval(self):
        identifier = str(uuid4())
        requests.queue(self.state, identifier, "mac", "What's on my screen?", "en", "UTC", screen=True)
        item = requests.inbox(self.state)["items"][0]
        with self.assertRaises(AccessDenied):
            requests.finish(self.state, identifier, "Too early", "screen")
        assistant = Mock(store=self.store)
        requests.approve(self.state, "iphone", identifier, self.proof("iphone", self.phone, "approve", item["subject"]), assistant)
        requests.finish(self.state, identifier, "A sample window", "screen")
        from nex.conversation.plan import validated
        plan = validated([{"name": "weather", "period": "current"}])
        assistant.generate_conversation = model(list(plan.tools))
        requests.queue(self.state, str(uuid4()), "iphone", "How's the weather outside?", "en", "UTC", plan=plan)
        local = requests.inbox(self.state)["items"][0]
        self.assertTrue(local["needs_location"])
        signed = self.proof("iphone", self.phone, "approve", local["subject"])
        with patch("nex.conversation.tools.Weather") as weather, patch("nex.conversation.tools.hourly", return_value="Sample forecast"):
            weather.return_value.locate.return_value = (object(), None)
            requests.approve(self.state, "iphone", local["id"], signed | {"latitude": 40.12, "longitude": -70.34}, assistant)
            self.assertEqual(weather.return_value.locate.call_args.kwargs["coordinates"].latitude, 40.12)
            self.assertEqual(requests.inbox(self.state)["items"][0]["answer"], "Sample forecast")
