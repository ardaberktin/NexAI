"""Exercise native account administration across the authenticated TLS API."""

from uuid import uuid4

from nex.hosted.auth import Accounts, hash_token
from nex.hosted.management import AccountSettings
from nex.hosted.setup import setup_secret
from nex.privacy.keys import issue_setup
from nex.privacy.state import AccessDenied
from test_hosted_accounts import PASSWORD, HostedAccountFixture, public_key, signature

NEW_PASSWORD = "a new disposable password"


class HostedAccountSettingsTests(HostedAccountFixture):
    def call(self, login, workspace, path="", method="GET", body=None, status=200):
        return self.request(method, "auth/" + path, login["token"], body=body, workspace=workspace, status=status)

    def owner_login(self):
        self.accounts.create("owner", PASSWORD, owner=True)
        owner = self.login("owner")
        return owner, self.unlock(owner)

    def enroll(self, **extras):
        return {"username": "owner", "password": PASSWORD, "device": "iphone", "public": public_key(self.key)} | extras

    def test_legacy_owner_setup_requires_workspace_and_preserves_existing_recordings(self):
        legacy = self.keys["iphone"]
        identifier = str(uuid4())
        self.upload(legacy, identifier)
        legacy_setup = issue_setup(self.host.owner.privacy, "iphone")
        challenge = self.request("POST", "privacy/challenge", legacy, body={"purpose": "unlock"})
        proof = signature(self.key, challenge) | {"public": public_key(self.key), "setup": legacy_setup}
        workspace = self.request("POST", "privacy/unlock", legacy, body=proof)["token"]
        self.request("POST", "auth/setup", body=self.enroll(), status=403)
        self.request("POST", "auth/setup", legacy, body=self.enroll(), status=403)
        self.request("POST", "auth/setup", legacy, workspace=workspace, body=self.enroll(device="mac"), status=403)
        owner = self.request("POST", "auth/setup", legacy, workspace=workspace, body=self.enroll())
        self.request("GET", "status", legacy, status=401)
        self.request("GET", "recordings", owner["token"], workspace=workspace, status=403)
        unlocked = self.unlock(owner)
        owner_profile = self.call(owner, unlocked, "profile")
        self.assertTrue(owner_profile["admin"])
        self.assertTrue(owner_profile["owner"])
        self.assertEqual(self.request("GET", "recordings", owner["token"], workspace=unlocked)["items"][0]["id"],
                         identifier)

    def test_private_setup_link_is_hash_only_expires_rotates_and_can_be_used_once(self):
        expired = setup_secret(self.accounts)
        with self.accounts.connect() as db:
            self.assertEqual(db.execute("SELECT token FROM owner_setup").fetchone()[0], hash_token(expired))
            db.execute("UPDATE owner_setup SET expires=0")
        self.request("POST", "auth/setup", body=self.enroll(setup=expired), status=403)
        superseded = setup_secret(self.accounts)
        valid = setup_secret(self.accounts)
        self.request("POST", "auth/setup", body=self.enroll(setup=superseded), status=403)
        first = self.request("POST", "auth/setup", body=self.enroll(setup=valid))
        self.assertTrue(first["owner"])
        self.request("POST", "auth/setup", body=self.enroll(setup=valid), status=403)
        with self.assertRaises(ValueError):
            setup_secret(self.accounts)
        with self.accounts.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM owner_setup").fetchone())

    def test_profile_rename_password_change_and_signing_out_another_device(self):
        self.accounts.create("member", PASSWORD)
        member = self.login("member")
        mac = self.login("member", device="mac")
        workspace = self.unlock(member)
        self.call(member, None, "profile", status=403)
        profile = self.call(member, workspace, "profile")
        self.assertEqual({row["role"] for row in profile["devices"]}, {"mac", "iphone"})
        self.call(member, workspace, "profile/revoke-device", "POST", {"device_id": member["device_id"]}, status=400)
        profile = self.call(member, workspace, "profile/revoke-device", "POST", {"device_id": mac["device_id"]})
        self.assertEqual(len(profile["devices"]), 1)
        self.request("GET", "status", mac["token"], status=401)
        mac = self.login("member", device="mac")
        self.call(member, workspace, "profile", "PUT", {"username": "bad value"}, status=400)
        renamed = self.call(member, workspace, "profile", "PUT", {"username": "Member.Edited"})
        self.assertEqual(renamed["id"], member["account_id"])
        self.assertEqual(renamed["username"], "member.edited")
        self.login("member", status=401)
        self.call(member, workspace, "profile/password", "PUT",
                  {"current_password": "wrong", "password": NEW_PASSWORD}, status=403)
        updated = self.call(member, workspace, "profile/password", "PUT",
                            {"current_password": PASSWORD, "password": NEW_PASSWORD})
        self.assertTrue(updated["devices"][0]["current"])
        self.request("GET", "status", mac["token"], status=401)
        self.call(member, workspace, "profile")
        self.login("member.edited", password=PASSWORD, status=401)
        self.login("member.edited", password=NEW_PASSWORD, device="mac")

    def test_administrator_scope_member_reset_and_separate_quota(self):
        owner, owner_ws = self.owner_login()
        delegate = self.call(owner, owner_ws, "users", "POST", {"username": "delegate", "password": PASSWORD})
        person = self.call(owner, owner_ws, "users", "POST", {"username": "member", "password": PASSWORD})
        officer = self.login("delegate")
        officer_ws = self.unlock(officer)
        target = "users/" + person["id"]
        officer_path = "users/" + delegate["id"]
        owner_path = "users/" + owner["account_id"]
        self.call(officer, officer_ws, "users", status=403)
        self.call(owner, None, "users", status=403)
        self.call(owner, owner_ws, officer_path, "PUT", {"username": "delegate", "admin": True, "enabled": True})
        self.assertEqual(len(self.call(officer, officer_ws, "users")["items"]), 3)
        self.call(officer, officer_ws, target, "PUT", {"username": "member", "admin": True, "enabled": True}, status=403)
        self.call(officer, officer_ws, officer_path, "PUT",
                  {"username": "delegate", "admin": False, "enabled": False}, status=403)
        self.call(officer, officer_ws, owner_path + "/password", "PUT", {"password": NEW_PASSWORD}, status=403)
        self.call(officer, officer_ws, owner_path + "/storage", status=403)
        before = self.call(owner, owner_ws, owner_path + "/storage")["audio_limit_bytes"]
        limit = self.call(officer, officer_ws, target + "/storage", "PUT", {"audio_limit_gib": 6})
        self.assertEqual(limit["audio_limit_bytes"], 6 * 2**30)
        self.assertEqual(self.call(owner, owner_ws, owner_path + "/storage")["audio_limit_bytes"], before)

        member = self.login("member")
        self.call(officer, officer_ws, target + "/password", "PUT", {"password": NEW_PASSWORD})
        self.request("GET", "status", member["token"], status=401)
        member = self.login("member", password=NEW_PASSWORD)
        self.call(officer, officer_ws, target, "PUT", {"username": "renamed", "admin": False, "enabled": False})
        self.request("GET", "status", member["token"], status=401)
        self.call(officer, officer_ws, target, "PUT", {"username": "renamed", "admin": False, "enabled": True})
        previously_authorized = self.accounts.authenticate(officer["token"])
        self.call(owner, owner_ws, officer_path, "PUT", {"username": "delegate", "admin": False, "enabled": True})
        with self.assertRaises(AccessDenied):
            AccountSettings(self.accounts, previously_authorized, "test").users()
        self.assertFalse(self.call(officer, officer_ws, "profile")["admin"])
        self.assertEqual(self.login("renamed", password=NEW_PASSWORD)["account_id"], person["id"])

    def test_existing_registry_migrates_without_revoking_devices(self):
        self.accounts.create("owner", PASSWORD, owner=True)
        first = self.login("owner")
        with self.accounts.connect() as db:
            db.execute("ALTER TABLE users DROP COLUMN admin")
        updated = Accounts(self.host.root)
        kept = updated.authenticate(first["token"])
        self.assertEqual(kept.account.id, first["account_id"])
        self.assertTrue(kept.account.can_manage)
        updated.create("member", PASSWORD)
        self.assertFalse(updated.list()[0][0].can_manage)
