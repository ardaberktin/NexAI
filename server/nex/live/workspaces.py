"""Keep live state per account while sharing the host's model and speech assets."""

import asyncio
import hmac
import logging
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

from ..activities.worker import ActivityWorker
from ..daybook_briefing import Briefings
from ..hosted.auth import Accounts
from ..live_memory import LiveMemory
from ..personal_files import PersonalFiles
from ..personal_learning import PersonalLearning
from ..privacy.recorded import speaker_socket
from .attention import Attention
from .daybook_socket import daybook_socket
from .dialogue import Dialogue
from .voice_profiles import VoiceProfiles

LOG = logging.getLogger(__name__)


class LiveWorkspaces:
    def __init__(self, root, runtime, admin_key, speech, room, verifier, model_socket, mac_socket):
        self.root, self.runtime, self.admin_key = root, runtime, admin_key
        self.accounts = Accounts(root)
        self.speech, self.room, self.verifier = speech, room, verifier
        self.model_socket, self.mac_socket = model_socket, mac_socket
        self.active, self.build_lock = asyncio.Lock(), asyncio.Lock()
        self.services = AsyncExitStack()
        self.workspaces = {}
        self.owner = None
        self.users = {}

    @asynccontextmanager
    async def running(self):
        async with self.services:
            self.owner = await self.services.enter_async_context(self.open_workspace(self.root))
            # Archive requests from extra accounts can only reach the server's own model.
            hosted_model = Dialogue(None, self.model_socket, None)
            self.services.push_async_callback(hosted_model.close)
            await self.services.enter_async_context(daybook_socket(self.runtime / "hosted-model.sock", hosted_model))
            monitor = asyncio.create_task(self.monitor())
            try:
                yield
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    def enabled(self, identifier):
        return self.users.get(identifier, False)

    async def monitor(self):
        while True:
            try:
                users = await asyncio.to_thread(self.accounts.list)
                self.users = {user.id: enabled for user, enabled in users}
                self.owner.enabled = next((enabled for user, enabled in users if user.owner), True)
                for account, enabled in users:
                    if enabled and not account.owner:
                        await self.workspace(account)
            except (OSError, RuntimeError, sqlite3.Error) as error:
                LOG.warning("Live workspace initialization deferred (%s)", type(error).__name__)
            await asyncio.sleep(2)

    async def workspace(self, account):
        if account.owner:
            return self.owner
        async with self.build_lock:
            if account.id not in self.workspaces:
                root = account.root(self.root)
                if not (root / "memory.sqlite").is_file():
                    return None
                state = await self.services.enter_async_context(self.open_workspace(root, account=account))
                self.workspaces[account.id] = state
            return self.workspaces[account.id]

    @asynccontextmanager
    async def open_workspace(self, root, *, account=None):
        memory = LiveMemory(root)
        with memory.connect() as db:
            db.execute("SELECT 1 FROM live_turn_results LIMIT 1")
        dialogue = Dialogue(memory, self.model_socket, None if account else self.mac_socket)
        async with AsyncExitStack() as stack:
            stack.push_async_callback(dialogue.close)
            if not account:
                await dialogue.wait_ready()
                await stack.enter_async_context(daybook_socket(self.runtime / "daybook.sock", dialogue))
            secret = self.admin_key
            if account:
                secret = hmac.digest(secret.encode(), account.id.encode(), "sha256").hex()
            voices = VoiceProfiles(memory.privacy, self.room, secret)
            path = account.speaker_socket(self.runtime) if account else self.runtime / "speaker.sock"
            await stack.enter_async_context(speaker_socket(path, voices))
            state = SimpleNamespace(
                speech=self.speech, room=self.room, verifier=self.verifier, dialogue=dialogue, voices=voices,
                attention=Attention(self.speech, dialogue, self.verifier, room=self.room), active=self.active,
            )
            enabled = (lambda: self.enabled(account.id)) if account else (lambda: state.enabled)
            if not account:
                state.enabled = next((enabled for user, enabled in self.accounts.list() if user.owner), True)
            tasks = (
                Briefings(memory, dialogue.background_text).run,
                ActivityWorker(memory, self.runtime, self.room, dialogue, settings_root=self.root, voices=voices).run,
                PersonalLearning(memory, PersonalFiles(root), dialogue.background_text).run,
            )
            jobs = asyncio.create_task(self.run_jobs(tasks, enabled))
            try:
                yield state
            finally:
                jobs.cancel()
                await asyncio.gather(jobs, return_exceptions=True)

    @staticmethod
    async def run_jobs(functions, enabled):
        while True:
            if not enabled():
                await asyncio.sleep(2)
                continue
            tasks = [asyncio.create_task(run()) for run in functions]
            try:
                while enabled() and not any(task.done() for task in tasks):
                    await asyncio.sleep(2)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
