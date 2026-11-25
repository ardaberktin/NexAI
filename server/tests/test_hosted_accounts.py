"""Exercise hosted sign-in through TLS and verify tenant boundaries at API entry points."""

import base64
import json
import ssl
import subprocess
import tempfile
import unittest
from http.client import HTTPSConnection
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nex.__main__ import read_access
from nex.hosted.auth import Accounts, hash_token
from nex.hosted.workspaces import HostedWorkspaces
from nex.http_api import Server
from nex.initialize import initialize
from nex.live.access import LiveAccess, authenticate, workspace_for
from nex.live.model_api import install_model_api
from nex.privacy.requests import queue
from nex.push_queue import PushQueue
from nex.store import now

PASSWORD = "disposable test password"
AUDIO = b"\x00\x00\x00\x20ftyp" + bytes(24)


def public_key(key):
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )).decode()


def signature(key, challenge):
    signed = key.sign(challenge["message"].encode(), ec.ECDSA(hashes.SHA256()))
    return {"nonce": challenge["nonce"], "signature": base64.b64encode(signed).decode()}


class HostedAccountFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            key, certificate = directory / "tls.key", directory / "tls.crt"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost"],
                capture_output=True, check=True, timeout=15,
            )
            cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            cls.tls.minimum_version = ssl.TLSVersion.TLSv1_2
            cls.tls.load_cert_chain(certificate, key)
            cls.trust = ssl.create_default_context(cafile=str(certificate))

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "archive"
        initialize(root, 1, 0)
        ending = Event()
        config = read_access(root)
        self.host = HostedWorkspaces(root, root.parent / "nex", config, ending)
        # Exercise real storage and transport without starting model, push, and scheduled workers.
        self.host.start_jobs = Mock()
        self.addCleanup(self.host.close)
        self.keys = config["keys"]
        self.accounts = self.host.accounts
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.user = ec.generate_private_key(ec.SECP256R1())
        self.server = Server(("127.0.0.1", 0), self.host.owner, tls=self.tls, hosted=self.host)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 4)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, token=None, *, body=None, workspace=None, status=200, headers=None):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = "Bearer " + token
        if workspace:
            headers["X-Nex-Workspace"] = workspace
        if isinstance(body, dict):
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection = HTTPSConnection("localhost", self.server.server_port, timeout=6, context=self.trust)
        try:
            connection.request(method, "/v2/" + path, body, headers)
            response = connection.getresponse()
            data = response.read()
            self.assertEqual(response.status, status, data)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return json.loads(data) if response.getheader("Content-Type").startswith("application/json") else data
        finally:
            connection.close()

    def login(self, username, *, device="iphone", key=None, password=PASSWORD, status=200):
        return self.request("POST", "auth/login", status=status, body={
            "username": username, "password": password, "device": device, "public": public_key(key or self.key)
        })

    def unlock(self, login, key=None):
        token = login["token"]
        challenge = self.request("POST", "privacy/challenge", token, body={"purpose": "unlock"})
        self.assertIn(login["device_id"], challenge["message"])
        return self.request("POST", "privacy/unlock", token, body=signature(key or self.key, challenge))["token"]

    def upload(self, token, identifier, audio=AUDIO, *, watch=False, status=201):
        path = ("watch/" if watch else "") + "recordings/" + identifier
        return self.request("PUT", path, token, status=status, body=audio, headers={
            "Content-Type": "audio/mp4", "X-Nex-Started": "2026-01-01T00:00:00Z",
            "X-Nex-Duration": "1.0", "X-Nex-Mode": "ambient",
        })



class HostedAccountTests(HostedAccountFixture):
    def test_hosted_sign_in_requires_a_tls_listener(self):
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            Server(("127.0.0.1", 0), self.host.owner, hosted=self.host)

    def test_archive_owner_adoption_and_same_ids_stay_in_separate_accounts(self):
        legacy, identifier = self.keys["iphone"], str(uuid4())
        self.upload(legacy, identifier)
        self.accounts.create("Owner", PASSWORD, owner=True)
        self.accounts.create("guest", PASSWORD)
        self.request("GET", "status", legacy, status=401)
        self.request("POST", "auth/logout", self.keys["admin"], status=401)
        owner = self.login("oWnEr")
        guest = self.login("guest", key=self.user)
        owner_workspace = self.unlock(owner)
        guest_workspace = self.unlock(guest, self.user)
        owner_token, guest_token = owner["token"], guest["token"]
        self.assertNotEqual(owner["account_id"], guest["account_id"])
        self.assertTrue(owner["owner"])
        self.assertFalse(guest["owner"])
        self.assertEqual(self.request("GET", "recordings", owner_token, workspace=owner_workspace)["items"][0]["id"],
                         identifier)
        self.assertEqual(self.request("GET", "recordings", guest_token, workspace=guest_workspace)["items"], [])
        self.request("GET", "recordings", guest_token, workspace=owner_workspace, status=403)
        self.request("GET", "recordings/" + identifier + "/audio", guest_token, workspace=guest_workspace, status=404)
        guest_audio = AUDIO[:-1] + b"g"
        self.upload(guest_token, identifier, guest_audio, watch=True)
        self.upload(guest_token, identifier, guest_audio, watch=True, status=200)
        self.request("GET", "watch/recordings/" + identifier + "/receipt", owner_token, status=404)
        self.assertEqual(
            self.request("GET", "recordings/" + identifier + "/audio", guest_token, workspace=guest_workspace),
            guest_audio,
        )
        self.assertEqual(
            self.request("GET", "recordings/" + identifier + "/audio", owner_token, workspace=owner_workspace), AUDIO,
        )

        guest_account = self.accounts.authenticate(guest_token).account
        store = self.host.workspace(guest_account).store
        self.assertEqual(store.root, self.host.root / "users" / guest["account_id"])
        self.assertIs(self.host.workspace(self.accounts.authenticate(owner_token).account), self.host.owner)
        with store.connect() as db:
            store.add_memory(db, "q-" + identifier, now(), "question", "iphone", "guestsentinel")
        question = str(uuid4())
        queue(store.privacy, question, "iphone", "Private guest question", "en", "UTC")
        self.assertEqual(len(self.request("GET", "memory?q=guestsentinel", guest_token, workspace=guest_workspace)["items"]), 1)
        self.assertEqual(self.request("GET", "memory?q=guestsentinel", owner_token, workspace=owner_workspace)["items"], [])
        self.assertEqual(self.request("GET", "privacy/inbox", owner_token, workspace=owner_workspace)["items"], [])
        self.assertEqual(self.request("GET", "privacy/inbox", guest_token, workspace=guest_workspace)["items"][0]["id"],
                         question)
        self.request("GET", "admin/models", guest_token, workspace=guest_workspace, status=404)
        self.assertIsNone(self.host.hosted_engines.route({"llama_server": "", "use_mac_model": True}))

    def test_relogin_logout_disable_reset_expiration_and_biometric_replay(self):
        self.accounts.create("guest", PASSWORD)
        first = self.login("guest")
        mac = self.login("guest", device="mac")
        old_token = first["token"]
        workspace = self.unlock(first)
        old_challenge = self.request("POST", "privacy/challenge", old_token, body={"purpose": "unlock"})
        newer = self.login("guest", key=self.user)
        new_token = newer["token"]
        self.assertEqual(first["account_id"], newer["account_id"])
        self.assertNotEqual(first["device_id"], newer["device_id"])
        with self.accounts.connect() as db:
            credentials = db.execute("SELECT * FROM devices WHERE id=?", (newer["device_id"],)).fetchone()
        self.assertEqual(credentials["token"], hash_token(new_token))
        self.request("GET", "status", old_token, status=401)
        self.request("GET", "status", mac["token"])
        self.request("GET", "recordings", new_token, workspace=workspace, status=403)
        self.request("POST", "privacy/unlock", new_token, body=signature(self.key, old_challenge), status=403)
        current = self.request("POST", "privacy/challenge", new_token, body={"purpose": "unlock"})
        self.request("POST", "privacy/unlock", new_token, body=signature(self.key, current), status=403)
        proof = signature(self.user, current)
        workspace = self.request("POST", "privacy/unlock", new_token, body=proof)["token"]
        self.request("POST", "privacy/unlock", new_token, body=proof, status=403)
        self.request("GET", "recordings", new_token, workspace=workspace)
        self.request("POST", "auth/logout", new_token)
        self.request("GET", "status", new_token, status=401)
        self.accounts.set_enabled("guest", False)
        self.request("GET", "status", mac["token"], status=401)
        self.login("guest", status=401)
        self.accounts.set_enabled("guest", True)
        previous = self.login("guest")
        self.accounts.reset("guest", "a new disposable password")
        self.request("GET", "status", previous["token"], status=401)
        self.login("guest", status=401)
        fresh = self.login("guest", password="a new disposable password")
        with self.accounts.connect() as db:
            db.execute("UPDATE devices SET expires=0 WHERE id=?", (fresh["device_id"],))
        self.request("GET", "status", fresh["token"], status=401)

    def test_bad_logins_are_generic_and_rate_limit_persists(self):
        self.accounts.create("guest", PASSWORD)
        expected = self.login("missing", status=401)
        for _ in range(7):
            self.assertEqual(self.login("guest", password="wrong password", status=401), expected)
        # Both daemons and a restarted API see the same auth database.
        self.host.accounts = Accounts(self.host.root)
        self.login("guest", status=429)

    def test_live_dispatch_uses_current_account_and_closes_revoked_socket(self):
        self.accounts.create("owner", PASSWORD, owner=True)
        self.accounts.create("guest", PASSWORD)
        owner, guest = self.login("owner"), self.login("guest")
        app = FastAPI()
        app.state.models = Mock()
        workspaces = SimpleNamespace(accounts=self.accounts, keys=self.keys)

        async def select(account):
            return self.host.workspace(account) if account else self.host.owner

        workspaces.workspace = select
        app.add_middleware(LiveAccess, workspaces=lambda: workspaces, loopback=True)
        install_model_api(app, authenticate)

        @app.get("/status")
        async def live_status(request: Request):
            return {"role": authenticate(request), "root": workspace_for(request).store.root.name
                    if authenticate(request) else None}

        @app.websocket("/voice")
        async def voice(ws: WebSocket):
            if authenticate(ws) is None:
                await ws.close(1008)
                return
            await ws.accept()
            await ws.send_text(workspace_for(ws).store.root.name)
            await ws.receive_text()

        def headers(login):
            return {"Authorization": "Bearer " + login["token"]}

        with TestClient(app) as client:
            self.assertEqual(client.get("/status", headers=headers(owner)).json()["root"], self.host.root.name)
            self.assertEqual(client.get("/status", headers=headers(guest)).json()["root"], guest["account_id"])
            self.assertEqual(client.put("/v2/live-model", json={"model": "test"}, headers=headers(guest)).status_code, 403)
            with client.websocket_connect("/voice", headers=headers(guest)) as socket:
                self.assertEqual(socket.receive_text(), guest["account_id"])
                self.request("POST", "auth/logout", guest["token"])
                with self.assertRaises(WebSocketDisconnect) as closed:
                    socket.receive_text()
                self.assertEqual(closed.exception.code, 1008)
            self.assertIsNone(client.get("/status", headers=headers(guest)).json()["root"])

    def test_revoked_iphone_registration_stops_push_delivery(self):
        self.accounts.create("owner", PASSWORD, owner=True)
        login = self.login("owner")
        store = self.host.owner.store
        push = PushQueue(store)
        installation = str(uuid4())
        with patch("nex.push_queue.Provider.load", return_value=SimpleNamespace(environment="development")):
            push.register(installation, "a" * 64, "development", True, device_id=login["device_id"])
        self.assertTrue(push.registration_current())
        self.request("POST", "auth/logout", login["token"])
        self.assertFalse(push.registration_current())
        with store.connect() as db:
            self.assertIsNone(db.execute("SELECT * FROM iphone_push_registration").fetchone())
