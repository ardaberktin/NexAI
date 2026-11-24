import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from nex.accounts.app_signin import AppSignIn
from nex.accounts.credentials import SCOPES, TICK_TASKS
from nex.accounts.http import AccountError
from nex.accounts.setup import TICK_REDIRECT

GOOGLE_ID = "123-nex.apps.googleusercontent.com"


def callback(attempt, **values):
    return attempt["redirect"] + "?" + urlencode({"state": attempt["state"], "code": "provider-code"} | values)


class AppSignInTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.sign_in = AppSignIn(temp)
        self.accounts = Path(temp) / "accounts"

    def test_ticktick_authorization_only_finishes_on_originating_device(self):
        with patch("nex.accounts.app_signin.ticktick_client", return_value={"client_id": "nex"}) as register:
            attempt = self.sign_in.start("ticktick", None, "iphone")
            other = self.sign_in.start("ticktick", None, "mac")
        register.assert_called_once_with(TICK_REDIRECT)
        self.assertNotEqual(attempt["state"], other["state"])
        query = parse_qs(urlsplit(attempt["url"]).query)
        self.assertEqual(query["scope"], [TICK_TASKS])
        self.assertEqual(query["redirect_uri"], [TICK_REDIRECT])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        for device, url in (
            ("mac", callback(attempt)),
            ("iphone", callback(attempt, state="wrong")),
            ("iphone", callback(attempt).replace(":/callback", ":/wrong")),
        ):
            with self.assertRaises(AccountError):
                self.sign_in.finish("ticktick", url, device)
        token = {"access_token": "token", "refresh_token": "refresh", "scope": TICK_TASKS}
        with patch("nex.accounts.setup.request", return_value=(token, {})) as exchange:
            result = self.sign_in.finish("ticktick", callback(attempt), "iphone")
        form = exchange.call_args.kwargs["form"]
        verifier_digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        self.assertEqual(base64.urlsafe_b64encode(verifier_digest).decode().rstrip("="), query["code_challenge"][0])
        self.assertEqual(form["redirect_uri"], TICK_REDIRECT)
        self.assertEqual(result["connections"]["ticktick"], "connected")
        self.assertEqual((self.accounts / "ticktick.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(AccountError):
            self.sign_in.finish("ticktick", callback(attempt), "iphone")

    def test_google_native_client_and_grant_are_shared(self):
        self.assertFalse(self.sign_in.options("iphone")["google_native"])
        self.sign_in.set_google_client({"client_id": GOOGLE_ID, "desktop": False}, "mac")
        self.assertTrue(self.sign_in.options("iphone")["google_native"])
        attempt = self.sign_in.start("google", None, "iphone")
        self.assertEqual(attempt["redirect"], "com.googleusercontent.apps.123-nex:/oauth2redirect")
        query = parse_qs(urlsplit(attempt["url"]).query)
        self.assertEqual(set(query["scope"][0].split()), SCOPES["google"])
        self.assertEqual(query["access_type"], ["offline"])
        token = {"access_token": "token", "refresh_token": "refresh", "scope": " ".join(SCOPES["google"])}
        with patch("nex.accounts.setup.request", return_value=(token, {})) as exchange:
            result = self.sign_in.finish("google", callback(attempt), "iphone")
        self.assertNotIn("client_secret", exchange.call_args.kwargs["form"])
        self.assertEqual(result["connections"], {"gmail": "connected", "calendar": "connected", "ticktick": "disconnected"})

    def test_google_desktop_client_and_loopback_are_mac_only(self):
        desktop = {"client_id": GOOGLE_ID, "client_secret": "desktop-secret", "desktop": True}
        with self.assertRaises(AccountError):
            self.sign_in.set_google_client(desktop, "iphone")
        self.sign_in.set_google_client(desktop, "mac")
        self.assertEqual(self.sign_in.options("mac"), {"google_native": False, "google_desktop": True})
        self.assertFalse(self.sign_in.options("iphone")["google_desktop"])
        with self.assertRaises(AccountError):
            self.sign_in.start("google", 49719, "iphone")
        with self.assertRaises(AccountError):
            self.sign_in.start("google", 80, "mac")
        attempt = self.sign_in.start("google", 49719, "mac")
        self.assertEqual(attempt["redirect"], "http://127.0.0.1:49719/callback")
        self.assertEqual((self.accounts / "google-desktop-client.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(AccountError):
            self.sign_in.finish("google", callback(attempt, error="access_denied"), "mac")
        self.assertEqual(self.sign_in.vault.status()["gmail"], "disconnected")
