"""Keep narrowly scoped OAuth grants on the server."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time

from .http import TOKEN, AccountError, request

GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
CALENDARS = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
EVENTS = "https://www.googleapis.com/auth/calendar.events.readonly"
TICK_TASKS = "tasks:read"
SCOPES = {"google": {GMAIL, CALENDARS, EVENTS}, "ticktick": {TICK_TASKS}}
TOKEN_URLS = {"google": "https://oauth2.googleapis.com/token", "ticktick": "https://api.ticktick.com/oauth/token"}
LABELS = {"gmail": "Gmail", "calendar": "Google Calendar", "ticktick": "TickTick"}


class Credentials:
    def __init__(self, root):
        self.root = Path(root) / "accounts"

    @contextmanager
    def locked(self, provider):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / (provider + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self, provider):
        try:
            value = json.loads((self.root / (provider + ".json")).read_text())
            if not isinstance(value, dict):
                return {}
            scopes = set(value.get("scopes", ()))
            valid = (scopes and scopes <= SCOPES[provider] and isinstance(value.get("expires_at"), (int, float))
                     and isinstance(value.get("access_token"), str) and TOKEN.fullmatch(value["access_token"]))
            return value if valid else {}
        except (OSError, ValueError, TypeError):
            return {}

    def write(self, provider, value):
        with NamedTemporaryFile(mode="w", dir=self.root, prefix=".grant-", delete=False) as file:
            name = Path(file.name)
            try:
                json.dump(value, file)
                file.flush()
                os.fsync(file.fileno())
                os.replace(name, self.root / (provider + ".json"))
            finally:
                name.unlink(missing_ok=True)

    def remove(self, provider):
        (self.root / (provider + ".json")).unlink(missing_ok=True)

    def accept(self, provider, client, response, *, prior=None):
        access = response.get("access_token") if isinstance(response, dict) else None
        if not isinstance(access, str) or not TOKEN.fullmatch(access):
            raise AccountError("The provider did not return an access token.")
        raw_scope = response.get("scope")
        scopes = set(raw_scope.split()) if isinstance(raw_scope, str) else set(
            (prior or {}).get("scopes", SCOPES[provider]),
        )
        if not scopes or not scopes <= SCOPES[provider]:
            raise AccountError("The provider returned broader permissions. Nex discarded the grant.")
        value = dict(client) | {"access_token": access, "scopes": sorted(scopes)}
        refresh = response.get("refresh_token") or (prior or {}).get("refresh_token")
        if isinstance(refresh, str) and TOKEN.fullmatch(refresh):
            value["refresh_token"] = refresh
        try:
            lifetime = int(response.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        value["expires_at"] = time() + lifetime
        return value

    def token(self, provider, scopes):
        with self.locked(provider):
            saved = self.read(provider)
            if not scopes <= set(saved.get("scopes", ())):
                raise AccountError("This account needs authorization on Excalibur.")
            if saved.get("expires_at", 0) > time() + 40:
                return saved["access_token"]
            refresh = saved.get("refresh_token")
            if not refresh:
                raise AccountError("The account session expired. Reconnect it on Excalibur.")
            client = {key: saved[key] for key in ("client_id", "client_secret") if key in saved}
            try:
                response, _ = request(TOKEN_URLS[provider], form={
                    **client, "grant_type": "refresh_token", "refresh_token": refresh,
                })
                updated = self.accept(provider, client, response, prior=saved)
            except AccountError:
                raise AccountError("The account needs to be reconnected on Excalibur.") from None
            if not scopes <= set(updated["scopes"]):
                raise AccountError("Nex did not receive the requested read permissions.")
            self.write(provider, updated)
            return updated["access_token"]

    def status(self):
        google = self.read("google")
        ticktick = self.read("ticktick")
        states = {}
        for name, saved, scopes in (("gmail", google, {GMAIL}), ("calendar", google, {CALENDARS, EVENTS}),
                                    ("ticktick", ticktick, {TICK_TASKS})):
            granted = scopes <= set(saved.get("scopes", ()))
            usable = saved.get("expires_at", 0) > time() or saved.get("refresh_token")
            states[name] = "connected" if granted and usable else "reconnect" if granted else "disconnected"
        return states
