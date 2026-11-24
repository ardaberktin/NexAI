import json
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nex.initialize import initialize
from nex.network import TailnetEndpoint


class TailnetEndpointTests(unittest.TestCase):
    def test_uses_tailscale_address_and_preserves_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = subprocess.CompletedProcess([], 0, stdout="100.64.10.20\n")
            with patch("nex.network.subprocess.run", return_value=result):
                endpoint = TailnetEndpoint.current(root)

            self.assertEqual(endpoint.address, "100.64.10.20")
            self.assertEqual(endpoint.identity, root / "tailnet-tls" / endpoint.address)
            endpoint.provision()
            original = endpoint.certificate.read_bytes()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(endpoint.certificate, endpoint.key)
            endpoint.provision()

            self.assertEqual(endpoint.certificate.read_bytes(), original)
            self.assertEqual(endpoint.key.stat().st_mode & 0o077, 0)

    def test_refuses_addresses_outside_the_tailnet_range(self):
        for address in ("192.168.0.2", "127.0.0.1", "100.128.0.1", ""):
            result = subprocess.CompletedProcess([], 0, stdout=address)
            with (self.subTest(address=address), patch("nex.network.subprocess.run", return_value=result),
                  self.assertRaises(ValueError)):
                TailnetEndpoint.current(Path("/unused"))


class ArchiveInitializationTests(unittest.TestCase):
    def test_creates_private_credentials_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "archive"
            initialize(root, 48, 2)
            access = root / "access.json"
            first = access.read_bytes()
            config = json.loads(first)
            self.assertEqual(config["audio_quota_bytes"], 48 * 2**30)
            self.assertEqual(config["min_free_bytes"], 2 * 2**30)
            self.assertEqual(set(config["keys"]), {"admin", "mac", "iphone"})
            self.assertEqual(len(set(config["keys"].values())), 3)
            for path in (root, root / "assets", access):
                self.assertEqual(path.stat().st_mode & 0o077, 0)
            initialize(root, 72, 6)
            self.assertEqual(access.read_bytes(), first)

    def test_refuses_a_shared_archive_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "shared"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "chmod 700"):
                initialize(root, 48, 2)
            self.assertFalse((root / "access.json").exists())
