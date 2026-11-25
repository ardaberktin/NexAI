"""Exercise personal data resets through the native API and the archive restart boundary."""

from threading import Event, Thread
from uuid import uuid4

from nex.hosted.reset import finish_resets, workspace_lock
from nex.hosted.setup import setup_secret
from test_hosted_accounts import PASSWORD, HostedAccountFixture


class AccountResetTests(HostedAccountFixture):
    def reset(self, login, *, session=None, password=PASSWORD, status=200):
        return self.request("POST", "auth/profile/reset-data", login["token"], workspace=session,
                            body={"current_password": password}, status=status)

    def test_member_can_erase_only_their_own_archive(self):
        self.accounts.create("owner", PASSWORD, owner=True)
        self.accounts.create("member", PASSWORD)
        owner = self.login("owner")
        member = self.login("member")
        self.upload(owner["token"], str(uuid4()))
        self.upload(member["token"], str(uuid4()))
        root = self.host.root
        member_root = root / "users" / member["account_id"]
        self.assertTrue((member_root / "memory.sqlite").is_file())
        member_mac = self.login("member", device="mac")

        self.reset(member, status=403)
        session = self.unlock(member)
        before = self.request("GET", "auth/profile", member["token"], workspace=session)
        self.assertTrue(before["can_reset_data"])
        self.request("POST", "auth/profile/reset-data", member["token"], workspace=session,
                     body={"current_password": PASSWORD, "user_id": owner["account_id"]}, status=400)
        self.reset(member, session=session, password="incorrect password", status=403)
        self.assertFalse(self.accounts.resets_pending())
        self.assertEqual(self.reset(member, session=session), {"queued": True})
        self.assertIsNone(self.accounts.authenticate(member["token"]))
        self.assertIsNone(self.accounts.authenticate(member_mac["token"]))
        self.assertIsNotNone(self.accounts.authenticate(owner["token"]))
        self.login("member", status=503)

        finish_resets(root, self.accounts, None)
        self.assertFalse(member_root.exists())
        self.assertFalse(self.accounts.resets_pending())
        self.assertEqual(self.host.owner.store.status()["recordings"], {"queued": 1})
        new_member = self.login("member")
        self.assertEqual(self.accounts.authenticate(new_member["token"]).account.id, before["id"])
        self.assertNotEqual(new_member["account_id"], member["account_id"])

    def test_owner_reset_waits_for_live_and_keeps_the_owner(self):
        self.accounts.create("owner", PASSWORD, owner=True)
        self.accounts.create("member", PASSWORD)
        owner = self.login("owner")
        member = self.login("member")
        recording_id = str(uuid4())
        self.upload(owner["token"], recording_id)
        self.upload(member["token"], str(uuid4()))
        root = self.host.root
        ssd = root.parent / "external-audio"
        ssd.mkdir()
        (ssd / (recording_id + ".m4a")).touch()
        unrelated_audio = ssd / (str(uuid4()) + ".m4a")
        unrelated_audio.touch()
        (root / "models.json").write_text("{}")
        waiting, finished = Event(), Event()

        def restart_archive():
            waiting.set()
            with workspace_lock(root, archive=True, ssd_audio=ssd):
                finished.set()

        with workspace_lock(root):
            self.reset(owner, session=self.unlock(owner))
            self.assertIsNone(self.accounts.authenticate(owner["token"]))
            self.assertIsNotNone(self.accounts.authenticate(member["token"]))
            worker = Thread(target=restart_archive, daemon=True)
            worker.start()
            self.assertTrue(waiting.wait(3))
            self.assertFalse(finished.wait(0.1))
            self.assertTrue((root / "memory.sqlite").exists())
        worker.join(5)
        self.assertTrue(finished.is_set())
        self.assertFalse((root / "memory.sqlite").exists())
        self.assertFalse((root / "privacy.sqlite").exists())
        self.assertEqual(list(ssd.iterdir()), [unrelated_audio])
        self.assertTrue((root / "models.json").is_file())
        self.assertTrue((root / "users" / member["account_id"] / "memory.sqlite").is_file())
        self.assertFalse(self.accounts.resets_pending())
        with self.assertRaisesRegex(ValueError, "already has an owner"):
            setup_secret(self.accounts)
        owner2 = self.login("owner", device="mac")
        self.assertTrue(owner2["owner"])
        self.assertEqual(self.accounts.authenticate(owner2["token"]).account.id, owner["account_id"])
        self.assertNotEqual(owner["account_id"], owner2["account_id"])

    def test_failed_reset_keeps_the_request_and_retries(self):
        self.accounts.create("owner", PASSWORD, owner=True)
        owner = self.login("owner")
        root = self.host.root
        ssd = root.parent / "external-audio"
        ssd.mkdir()
        recording_id = str(uuid4())
        self.upload(owner["token"], recording_id)
        blocker = ssd / (recording_id + ".m4a")
        blocker.mkdir()
        self.reset(owner, session=self.unlock(owner))

        with self.assertRaises(OSError):
            finish_resets(root, self.accounts, ssd)
        self.assertTrue(self.accounts.resets_pending())
        self.assertTrue((root / "memory.sqlite").is_file())
        self.login("owner", status=503)

        blocker.rmdir()
        finish_resets(root, self.accounts, ssd)
        self.assertFalse(self.accounts.resets_pending())
        self.assertFalse((root / "memory.sqlite").exists())
        self.assertTrue(self.login("owner")["owner"])
