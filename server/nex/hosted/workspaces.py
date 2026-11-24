"""Account-scoped archives and jobs behind a single HTTP listener."""

import logging
import threading
from types import SimpleNamespace

from ..accounts.app_signin import AppSignIn
from ..assistant import Assistant
from ..models import Engines
from ..privacy.chat_history import ChatFollowups
from ..settings import Settings
from ..store import Store
from ..worker import Worker
from ..workspace_jobs import start_workspace_jobs
from ..workspace_sync import RecordingPresence
from .auth import Accounts

LOG = logging.getLogger(__name__)


class HostedWorkspaces:
    def __init__(self, root, runtime, credentials, ending, *, ssd_audio=None):
        self.accounts = Accounts(root)
        self.root, self.credentials, self.ending = root, credentials, ending
        self.runtime, self.ssd_audio = runtime, ssd_audio
        self.settings = Settings(root)
        self.engines = Engines(self.settings, runtime)
        self.hosted_engines = Engines(self.settings, runtime / "hosted", allow_mac=False,
                                      live_socket=runtime.parent / "nex-live" / "hosted-model.sock")
        self.speech_slot = threading.Lock()
        self.lock = threading.Lock()
        self.workspaces = {}
        self.owner = self.build(root, self.engines, ssd_audio=ssd_audio)

    def build(self, root, engines, *, ssd_audio=None, account=None):
        store = Store(root, self.credentials["min_free_bytes"], self.credentials["audio_quota_bytes"],
                      ssd_audio=ssd_audio)
        store.hosted_accounts = self.accounts
        if account:
            store.speaker_socket = account.speaker_socket(self.runtime.parent / "nex-live")
        assistant = Assistant(store, engines, self.settings)
        enabled = threading.Event()
        worker = Worker(store, engines, assistant, self.settings, enabled=enabled.is_set)
        return SimpleNamespace(
            store=store, worker=worker, assistant=assistant, engines=engines, settings=self.settings,
            chat_slot=threading.Lock(), speech_slot=self.speech_slot, keys=self.credentials["keys"],
            sign_in=AppSignIn(root), privacy=store.privacy, chat_followups=ChatFollowups(),
            recording_status=RecordingPresence(store), enabled=enabled,
        )

    def start_jobs(self, app):
        app.worker.start()
        start_workspace_jobs(app.store, app.engines, self.settings, self.ending, enabled=app.enabled.is_set)
        threading.Thread(target=app.store.archive.maintain, args=(self.ending,),
                         name="audio-retention", daemon=True).start()

    def workspace(self, account):
        if account.owner:
            return self.owner
        with self.lock:
            if account.id not in self.workspaces:
                app = self.build(account.root(self.root), self.hosted_engines, account=account)
                self.start_jobs(app)
                self.workspaces[account.id] = app
            return self.workspaces[account.id]

    def update(self):
        accounts = self.accounts.list()
        owner_enabled = next((enabled for user, enabled in accounts if user.owner), True)
        if owner_enabled:
            self.owner.enabled.set()
        else:
            self.owner.enabled.clear()
        for account, enabled in accounts:
            if account.owner:
                continue
            if enabled:
                app = self.workspace(account)
            else:
                with self.lock:
                    app = self.workspaces.get(account.id)
            if app:
                (app.enabled.set if enabled else app.enabled.clear)()

    def configure(self):
        with self.lock:
            for app in (self.owner, *self.workspaces.values()):
                app.worker.configure()

    def start(self):
        self.update()
        self.start_jobs(self.owner)
        threading.Thread(target=self.monitor, name="account-workspaces", daemon=True).start()

    def monitor(self):
        while not self.ending.wait(2):
            try:
                self.update()
            except Exception as error:  # noqa: BLE001 - retry archive initialization after a temporary error.
                LOG.error("Could not refresh Nex account workspaces (%s)", type(error).__name__)

    def close(self):
        with self.lock:
            for app in (self.owner, *self.workspaces.values()):
                app.worker.stop.set()
                app.worker.wake.set()
        for engine in (self.engines, self.hosted_engines):
            engine.piper.close()
