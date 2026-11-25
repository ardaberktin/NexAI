import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from nex.accounts.app_signin import AppSignIn
from nex.accounts.credentials import EVENTS, GMAIL, SCOPES, TICK_TASKS, WRITE_SCOPES
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

    def test_google_write_consent_is_scoped_and_read_consent_is_preserved(self):
        self.sign_in.set_google_client({"client_id": GOOGLE_ID, "desktop": False}, "iphone")
        vault = self.sign_in.vault
        with vault.locked("google"):
            legacy = vault.accept("google", {"client_id": GOOGLE_ID}, {"access_token": "old-token"})
            vault.write("google", legacy)
        attempt = self.sign_in.start("google", None, "iphone", enable_write="calendar")
        scopes = SCOPES["google"] | {WRITE_SCOPES["calendar"]}
        self.assertEqual(set(parse_qs(urlsplit(attempt["url"]).query)["scope"][0].split()), scopes)
        partial = {"access_token": "partial-token", "scope": " ".join(SCOPES["google"])}
        with patch("nex.accounts.setup.request", return_value=(partial, {})), self.assertRaisesRegex(AccountError, "all requested"):
            self.sign_in.finish("google", callback(attempt), "iphone")
        self.assertEqual(vault.read("google"), legacy)
        retry = self.sign_in.start("google", None, "iphone", enable_write="calendar")
        accepted = {"access_token": "new-token", "scope": " ".join(scopes)}
        with patch("nex.accounts.setup.request", return_value=(accepted, {})):
            self.sign_in.finish("google", callback(retry), "iphone")
        self.assertEqual(vault.write_access(), {"calendar": False})
        self.assertEqual(vault.status()["calendar"], "connected")
        self.assertEqual(vault.token("google", {EVENTS}), "new-token")

    def test_ticktick_write_consent_and_revocation_invalidate_pending_sign_in(self):
        with patch("nex.accounts.app_signin.ticktick_client", return_value={"client_id": "nex-write"}) as register:
            attempt = self.sign_in.start("ticktick", None, "iphone", enable_write="ticktick")
        register.assert_called_once_with(TICK_REDIRECT, write=True)
        scopes = {TICK_TASKS, WRITE_SCOPES["ticktick"]}
        self.assertEqual(set(parse_qs(urlsplit(attempt["url"]).query)["scope"][0].split()), scopes)
        with patch("nex.accounts.setup.request", return_value=({"access_token": "write-token"}, {})):
            self.sign_in.finish("ticktick", callback(attempt), "iphone")
        self.assertEqual(self.sign_in.vault.write_access(), {"ticktick": False})
        self.sign_in.update_access({"account": "ticktick", "mode": "scheduled"})
        self.assertEqual(self.sign_in.vault.write_access(), {"ticktick": True})
        pending = self.sign_in.start("ticktick", None, "mac")
        self.sign_in.update_access({"account": "ticktick", "mode": "off"})
        self.assertEqual(self.sign_in.vault.write_access(), {})
        self.assertEqual(self.sign_in.vault.token("ticktick", {TICK_TASKS}), "write-token")
        with self.assertRaisesRegex(AccountError, "expired"):
            self.sign_in.finish("ticktick", callback(pending), "mac")
        with self.assertRaisesRegex(AccountError, "Authorize write access"):
            self.sign_in.update_access({"account": "ticktick", "mode": "scheduled"})

    def test_disconnect_preserves_legacy_google_setup_and_invalidates_pending_callbacks(self):
        vault = self.sign_in.vault
        client = {"client_id": GOOGLE_ID, "client_secret": "desktop-secret"}
        expired = {"access_token": "expired-google-token", "expires_in": -60}
        with vault.locked("google"):
            vault.write("google", vault.accept("google", client, expired))
        desktop = self.sign_in.start("google", 49719, "mac")
        self.sign_in.set_google_client({"client_id": GOOGLE_ID, "desktop": False}, "iphone")
        native = self.sign_in.start("google", None, "iphone")
        self.assertEqual(vault.status()["gmail"], "reconnect")

        result = self.sign_in.disconnect("google")
        self.assertEqual(result["connections"], dict.fromkeys(("gmail", "calendar", "ticktick"), "disconnected"))
        self.assertFalse((self.accounts / "google.json").exists())
        self.assertEqual(self.sign_in.read_client("google-desktop-client"), client)
        self.assertTrue(self.sign_in.options("iphone")["google_native"])
        with self.assertRaises(AccountError):
            vault.token("google", {GMAIL})
        for device, attempt in (("mac", desktop), ("iphone", native)):
            with self.assertRaisesRegex(AccountError, "expired"):
                self.sign_in.finish("google", callback(attempt), device)

        fresh = self.sign_in.start("google", 49719, "mac")
        with patch("nex.accounts.setup.request", return_value=({"access_token": "new-token"}, {})):
            self.assertEqual(self.sign_in.finish("google", callback(fresh), "mac")["connections"]["gmail"], "connected")

    def test_disconnect_can_retry_after_a_sign_in_finishes(self):
        with patch("nex.accounts.app_signin.ticktick_client", return_value={"client_id": "nex"}):
            attempt = self.sign_in.start("ticktick", None, "iphone")

        def exchange(*_args, **_kwargs):
            with self.assertRaisesRegex(AccountError, "finishing another sign-in"):
                self.sign_in.disconnect("ticktick")
            return {"access_token": "ticktick-token"}, {}

        with patch("nex.accounts.setup.request", side_effect=exchange):
            self.assertEqual(self.sign_in.finish("ticktick", callback(attempt), "iphone")["connections"]["ticktick"],
                             "connected")
        self.assertEqual(self.sign_in.disconnect("ticktick")["connections"]["ticktick"], "disconnected")
        self.assertFalse((self.accounts / "ticktick.json").exists())
        self.assertEqual(self.sign_in.read_client("ticktick-native-client"), {"client_id": "nex"})
