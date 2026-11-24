import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

from nex.privacy import keys
from nex.privacy.state import PrivacyState

LIVE_DEPS = ("cryptography", "numpy", "fastapi", "httpx")


@unittest.skipUnless(all(importlib.util.find_spec(module) for module in LIVE_DEPS), "needs the live voice dependencies")
class VoiceEnrollmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        import numpy as np
        from fastapi import FastAPI
        from nex.live.voice_enrollment import install_voice_enrollment
        from nex.live.voice_profiles import SAMPLE_BYTES, VoiceProfiles

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = PrivacyState(Path(folder.name))
        self.signers = {}
        self.room = Mock(lock=Lock())
        self.room.speakers.process.return_value.sort_by_start_time.return_value = [SimpleNamespace(speaker=0, start=0, end=3)]
        self.room.has_media.return_value = False
        self.room.voice.return_value = np.array([1., 0.])
        app = FastAPI()
        self.voices = app.state.voices = VoiceProfiles(self.state, self.room, "test-only-secret")
        devices = {"Bearer test-mac": "mac", "Bearer test-iphone": "iphone"}
        install_voice_enrollment(app, lambda r: devices.get(r.headers.get("authorization")))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                        headers=self.unlock_device("mac"))
        self.addAsyncCleanup(self.client.aclose)
        self.sample = b"\x01\x00" * (SAMPLE_BYTES // 2)
        self.path = "/v2/voice/profile"

    def unlock_device(self, device):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        signer = ec.generate_private_key(ec.SECP256R1())
        self.signers[device] = signer
        public = signer.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        unlock = self.proof("unlock", device=device) | {
            "public": base64.b64encode(public).decode(), "setup": keys.issue_setup(self.state, device),
        }
        session = keys.unlock(self.state, device, unlock)["token"]
        return {"Authorization": f"Bearer test-{device}", "X-Nex-Workspace": session}

    def proof(self, purpose, subject="", *, device="mac"):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        challenge = keys.challenge(self.state, device, purpose, subject)
        sig = self.signers[device].sign(challenge["message"].encode(), ec.ECDSA(hashes.SHA256()))
        return {"nonce": challenge["nonce"], "signature": base64.b64encode(sig).decode()}

    def enrollment_payload(self, audio, *, device="mac"):
        from nex.live.voice_profiles import enrollment_subject

        return {"action": "enroll", "audio": base64.b64encode(audio).decode()} | self.proof(
            "voice", enrollment_subject(audio), device=device)

    async def test_accepts_slash_escaped_native_enrollment(self):
        audio = b"\xff" * (3 * len(self.sample))
        body = json.dumps(self.enrollment_payload(audio)).replace("/", r"\/").encode()
        self.assertGreater(len(body), 4 * len(self.sample) + 4_000)
        response = await self.client.post(self.path, content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"enrolled": True, "shared_enrolled": True, "available": True})

    async def test_paired_devices_share_status_and_manage_their_own_samples(self):
        phone = self.unlock_device("iphone")
        empty = {"enrolled": False, "shared_enrolled": False, "available": True}
        shared_only = empty | {"shared_enrolled": True}
        contributed = shared_only | {"enrolled": True}
        audio = self.sample * 3

        self.assertEqual((await self.client.get(self.path, headers=phone)).json(), empty)
        mac_enrollment = await self.client.post(self.path, json=self.enrollment_payload(audio))
        self.assertEqual(mac_enrollment.json(), contributed)
        self.assertEqual((await self.client.get(self.path, headers=phone)).json(), shared_only)

        phone_enrollment = await self.client.post(self.path, headers=phone,
                                                  json=self.enrollment_payload(audio, device="iphone"))
        self.assertEqual(phone_enrollment.json(), contributed)
        delete_mac = self.proof("voice", "delete") | {"action": "delete"}
        self.assertEqual((await self.client.post(self.path, json=delete_mac)).json(), shared_only)
        self.assertEqual((await self.client.get(self.path, headers=phone)).json(), contributed)

        delete_phone = self.proof("voice", "delete", device="iphone") | {"action": "delete"}
        self.assertEqual((await self.client.post(self.path, headers=phone, json=delete_phone)).json(), empty)
        self.assertEqual((await self.client.get(self.path)).json(), empty)

    async def test_enforces_audio_size_and_bounds_streamed_uploads(self):
        from nex.live.voice_enrollment import MAX_BODY_BYTES

        audio = self.sample * 3
        short = await self.client.post(self.path, json=self.enrollment_payload(audio[:-2]))
        self.assertEqual(short.status_code, 400, short.text)
        self.assertFalse((await self.client.get(self.path)).json()["enrolled"])

        async def chunks(first, second):
            yield first
            yield second

        body = json.dumps(self.enrollment_payload(audio)).encode()
        valid = await self.client.post(self.path, content=chunks(body[:1000], body[1000:]))
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertTrue(valid.json()["enrolled"])
        large = b" " * (MAX_BODY_BYTES + 1)
        for request_body in (large, chunks(large[:MAX_BODY_BYTES], large[MAX_BODY_BYTES:])):
            invalid = await self.client.post(self.path, content=request_body)
            self.assertEqual(invalid.status_code, 413, invalid.text)

    async def test_enrollment_is_bound_to_audio_and_removable_with_fresh_biometrics(self):
        import numpy as np
        from nex.live.voice_profiles import enrollment_subject

        audio = self.sample * 3
        payload = {"action": "enroll", "audio": base64.b64encode(audio).decode()}
        self.assertEqual((await self.client.get(self.path, headers={"X-Nex-Workspace": ""})).status_code, 403)
        wrong = self.proof("voice", enrollment_subject(audio[:-2]))
        self.assertEqual((await self.client.post(self.path, json=payload | wrong)).status_code, 403)
        signed = self.proof("voice", enrollment_subject(audio))
        self.assertTrue((await self.client.post(self.path, json=payload | signed)).json()["enrolled"])
        self.assertEqual((await self.client.post(self.path, json=payload | signed)).status_code, 403)
        with self.state.transaction() as db:
            template = db.execute("SELECT template FROM voices").fetchone()[0]
        self.assertNotIn(b"[1.0", template)
        self.assertEqual(self.voices.identify(self.sample), "recognized")
        self.room.voice.return_value = np.array([0., 1.])
        self.assertEqual(self.voices.identify(self.sample), "unverified")
        self.room.has_media.return_value = True
        self.assertEqual(self.voices.identify(self.sample), "unverified")
        with self.assertRaises(ValueError):
            self.voices.enrollment(audio)
        remove = self.proof("voice", "delete") | {"action": "delete"}
        self.assertFalse((await self.client.post(self.path, json=remove)).json()["enrolled"])
        self.assertFalse((await self.client.get(self.path)).json()["enrolled"])
        self.assertEqual(self.voices.identify(self.sample), "unverified")
