"""Verify that live voice uses separate archives, speaker sockets and in-memory state."""

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock, patch

from nex.__main__ import read_access
from nex.hosted.workspaces import HostedWorkspaces
from nex.initialize import initialize
from nex.live.workspaces import LiveWorkspaces
from nex.store import now


class HostedLiveWorkspacesTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_workspaces_share_assets_and_keep_state_in_their_account(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        root, runtime = directory / "archive", directory / "nex-live"
        runtime.mkdir(mode=0o700)
        initialize(root, 1, 0)
        config = read_access(root)
        host = HostedWorkspaces(root, directory / "nex", config, Event())
        self.addCleanup(host.close)
        host.start_jobs = Mock()
        host.accounts.create("owner", "disposable owner password", owner=True)
        host.accounts.create("guest", "disposable guest password")
        guest, owner = [user for user, _ in host.accounts.list()]
        guest_app = host.workspace(guest)
        with guest_app.store.connect() as db:
            guest_app.store.add_memory(db, "guest-entry", now(), "question", "iphone", "guest live sentinel")
        mac_socket = directory / "mac-model.sock"
        service = LiveWorkspaces(root, runtime, config["keys"]["admin"], Mock(), None, Mock(), runtime / "model.sock", mac_socket)
        self.enterContext(patch("nex.live.dialogue.Dialogue.wait_ready", new_callable=AsyncMock))
        self.enterContext(patch("nex.live.workspaces.LiveWorkspaces.run_jobs", new_callable=AsyncMock))
        async with service.running():
            owner_voice = await service.workspace(owner)
            guest_voice = await service.workspace(guest)
            self.assertIs(guest_voice, await service.workspace(guest))
            self.assertIs(guest_voice.speech, owner_voice.speech)
            self.assertIs(guest_voice.active, owner_voice.active)
            self.assertIsNot(guest_voice.attention.tickets, owner_voice.attention.tickets)
            self.assertIsNotNone(owner_voice.dialogue.mac)
            self.assertIsNone(guest_voice.dialogue.mac)
            self.assertEqual(guest_voice.voices.state.root, guest_app.store.root)
            self.assertEqual(guest_voice.dialogue.memory.source("guest-entry")["text"], "guest live sentinel")
            self.assertIsNone(owner_voice.dialogue.memory.source("guest-entry"))
            for account in (owner, guest):
                self.assertTrue(account.speaker_socket(runtime).is_socket())
            self.assertTrue((runtime / "hosted-model.sock").is_socket())
        self.assertFalse(list(runtime.glob("*.sock")))
