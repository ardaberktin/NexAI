"""Verify first-owner setup through the private listener and native sign-in API."""

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from threading import Thread

from nex.hosted.browser_setup import HOST, ORIGIN, BrowserSetupServer
from nex.hosted.setup import setup_secret
from test_hosted_accounts import PASSWORD, HostedAccountFixture, public_key

LINK = "nex://100.64.10.20:8765/" + "A" * 43


class SocketConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__(HOST, timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class BrowserSetupTests(HostedAccountFixture):
    def setUp(self):
        super().setUp()
        self.setup = BrowserSetupServer(self.host.root.parent / "runtime", self.host, LINK)
        self.addCleanup(self.setup.server_close)
        worker = Thread(target=self.setup.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 4)
        self.addCleanup(self.setup.shutdown)

    def browser(self, path="/setup", *, data=None, headers=None, status=200):
        options = {"Host": HOST, "Origin": ORIGIN, "Content-Type": "application/json"} | (headers or {})
        connection = SocketConnection(self.setup.path)
        try:
            try:
                connection.request("GET" if data is None else "POST", path, json.dumps(data) if data else None, options)
            except BrokenPipeError:
                # Origin and media-type checks can send a complete rejection before the client writes its body.
                if status not in {403, 415}:
                    raise
            response = connection.getresponse()
            payload = response.read()
            self.assertEqual(response.status, status, payload)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return json.loads(payload) if response.headers.get_content_type() == "application/json" else payload
        finally:
            connection.close()

    def test_browser_owner_can_sign_in_from_the_native_client(self):
        self.assertEqual(self.setup.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.setup.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn(b"Getting Started", self.browser("/"))
        self.assertIn(b"requestSetup", self.browser("/setup.js"))
        self.assertEqual(self.browser(), {"owner_exists": False, "server_link": None})
        setup = setup_secret(self.accounts)
        self.browser(data={"username": "Nora", "password": "short"}, status=400)
        result = self.browser(data={"username": "Nora", "password": PASSWORD}, status=201)
        self.assertEqual(result, {"username": "nora", "server_link": LINK})
        self.assertEqual(self.browser(), {"owner_exists": True, "server_link": LINK})
        self.assertTrue(self.login("nora")["owner"])
        self.browser(data={"username": "other", "password": PASSWORD}, status=409)
        self.request("POST", "auth/setup", body={"username": "other", "password": PASSWORD,
                     "device": "mac", "public": public_key(self.key), "setup": setup}, status=403)
        with self.accounts.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM owner_setup").fetchone())

    def test_setup_is_isolated_and_rejects_cross_origin_submissions(self):
        data = {"username": "nora", "password": PASSWORD}
        self.request("POST", "/setup", body=data, status=401)
        self.browser("/v1/status", status=404)
        self.browser(headers={"Host": "other.test"}, status=403)
        self.browser(data=data, headers={"Origin": "https://other.test"}, status=403)
        self.browser(data=data, headers={"Origin": ""}, status=403)
        self.browser(data=data, headers={"Content-Type": "text/plain"}, status=415)
        self.assertFalse(self.browser()["owner_exists"])

    def test_concurrent_setup_creates_one_owner(self):
        def claim(name):
            with closing(SocketConnection(self.setup.path)) as connection:
                data = json.dumps({"username": name, "password": PASSWORD})
                connection.request("POST", "/setup", data, {"Origin": ORIGIN, "Content-Type": "application/json"})
                response = connection.getresponse()
                response.read()
                return response.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(executor.map(claim, ["nora", "alex"]), [201, 409])
        self.assertEqual(len(self.accounts.list()), 1)
