"""Track OAuth sign-ins started by paired Nex devices."""

import json
import re
import threading
from dataclasses import dataclass
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from .credentials import SCOPES, Credentials
from .http import TOKEN, AccountError
from .setup import TICK_REDIRECT, Authorization, ticktick_client

GOOGLE_ID = re.compile(r"[a-zA-Z0-9_-]{1,120}\.apps\.googleusercontent\.com")
GOOGLE_NATIVE = "google-native-client"
GOOGLE_DESKTOP = "google-desktop-client"
TICK_CLIENT = "ticktick-native-client"
DEADLINE_SECONDS = 600


@dataclass(frozen=True)
class PendingSignIn:
    flow: Authorization
    expires: float


class AppSignIn:
    def __init__(self, root):
        self.vault = Credentials(root)
        self.lock = threading.Lock()
        self.pending = {}
        self.finishing = set()

    def read_client(self, name):
        try:
            client = json.loads((self.vault.root / (name + ".json")).read_text())
            return client if isinstance(client, dict) and client.get("client_id") else None
        except (OSError, ValueError):
            return None

    def google_desktop(self):
        client = self.read_client(GOOGLE_DESKTOP)
        if client:
            return client
        # Older connections authorized through the CLI used a Desktop client.
        return self.legacy_google_desktop()

    def legacy_google_desktop(self):
        prior = self.vault.read("google")
        if prior.get("client_secret") and prior.get("client_id"):
            return {key: prior[key] for key in ("client_id", "client_secret")}
        return None

    def options(self, device):
        return {"google_native": bool(self.read_client(GOOGLE_NATIVE)),
                "google_desktop": device == "mac" and bool(self.google_desktop())}

    def set_google_client(self, data, device):
        desktop = data.get("desktop")
        if type(desktop) is not bool or (desktop and device != "mac"):
            raise AccountError("Choose an OAuth client for this device.")
        client_id = data.get("client_id")
        if not isinstance(client_id, str) or not GOOGLE_ID.fullmatch(client_id):
            raise AccountError("Enter a Google OAuth client ID ending in apps.googleusercontent.com.")
        client = {"client_id": client_id}
        if desktop:
            secret = data.get("client_secret")
            if not isinstance(secret, str) or not TOKEN.fullmatch(secret):
                raise AccountError("Import the Desktop OAuth client JSON downloaded from Google Cloud.")
            client["client_secret"] = secret
        name = GOOGLE_DESKTOP if desktop else GOOGLE_NATIVE
        with self.vault.locked(name):
            self.vault.write(name, client)
        return self.options(device)

    def google_flow(self, port, device):
        if port is not None:
            if device != "mac" or type(port) is not int or not 1024 <= port <= 65535:
                raise AccountError("The Google Desktop client needs a local callback on the Mac.")
            client = self.google_desktop()
            if not client:
                raise AccountError("Import a Google Desktop OAuth client file first.")
            return Authorization("google", client, f"http://127.0.0.1:{port}/callback")

        client = self.read_client(GOOGLE_NATIVE)
        if not client:
            raise AccountError("Set up your Google iOS OAuth client first.")
        scheme = ".".join(reversed(client["client_id"].split(".")))
        return Authorization("google", client, scheme + ":/oauth2redirect")

    def start(self, provider, port, device):
        if provider == "google":
            flow = self.google_flow(port, device)
        elif provider == "ticktick" and port is None:
            with self.vault.locked(TICK_CLIENT):
                client = self.read_client(TICK_CLIENT)
                if not client:
                    client = ticktick_client(TICK_REDIRECT)
                    self.vault.write(TICK_CLIENT, client)
            flow = Authorization("ticktick", client, TICK_REDIRECT)
        else:
            raise AccountError("Choose Google or TickTick.")
        with self.lock:
            if provider in self.finishing:
                raise AccountError("Nex is finishing another sign-in for this account.")
            self.pending[provider, device] = PendingSignIn(flow, monotonic() + DEADLINE_SECONDS)
        return {"url": flow.url(), "state": flow.state, "redirect": flow.redirect}

    def finish(self, provider, callback, device):
        if provider not in ("google", "ticktick") or not isinstance(callback, str) or len(callback) > 10000:
            raise AccountError("Nex received an invalid sign-in response.")

        try:
            url = urlsplit(callback)
            values = parse_qs(url.query, max_num_fields=20, keep_blank_values=True)
        except ValueError:
            raise AccountError("Nex received an invalid sign-in response.") from None
        with self.lock:
            key = provider, device
            pending = self.pending.get(key)
            if pending is None or pending.expires < monotonic():
                raise AccountError("This sign-in expired. Try again from Nex Settings.")
            flow = pending.flow
            if values.get("state") != [flow.state] or url._replace(query="").geturl() != flow.redirect:
                raise AccountError("The sign-in response did not match this request.")
            if provider in self.finishing:
                raise AccountError("Nex is finishing another sign-in for this account.")
            self.pending.pop(key)
            self.finishing.add(provider)
        try:
            codes = values.get("code", [])
            if "error" in values or len(codes) != 1 or not TOKEN.fullmatch(codes[0]):
                raise AccountError("The provider did not complete sign-in.")
            flow.save(self.vault, codes[0])
            return {"connections": self.vault.status()}
        finally:
            with self.lock:
                self.finishing.discard(provider)

    def disconnect(self, provider):
        if not isinstance(provider, str) or provider not in SCOPES:
            raise AccountError("Choose Google or TickTick.")
        with self.lock:
            if provider in self.finishing:
                raise AccountError("Nex is finishing another sign-in for this account. Try again shortly.")
            with self.vault.locked(provider):
                if provider == "google":
                    self.preserve_google_desktop()
                self.vault.remove(provider)
            for key in list(self.pending):
                if key[0] == provider:
                    del self.pending[key]
            return {"connections": self.vault.status()}

    def preserve_google_desktop(self):
        with self.vault.locked(GOOGLE_DESKTOP):
            if not self.read_client(GOOGLE_DESKTOP) and (client := self.legacy_google_desktop()):
                self.vault.write(GOOGLE_DESKTOP, client)
