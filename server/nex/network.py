import base64
import fcntl
import hashlib
import ipaddress
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

TAILNET = ipaddress.IPv4Network("100.64.0.0/10")
TAILSCALE_INTERFACE = "tailscale0"
CLIENT_PORT = 8765


@dataclass(frozen=True)
class TailnetEndpoint:
    address: str
    identity: Path

    @classmethod
    def current(cls, root):
        result = subprocess.run(["/usr/bin/tailscale", "ip", "-4"], check=True,
                                capture_output=True, text=True, timeout=8)
        address = ipaddress.IPv4Address(result.stdout.strip())
        if address not in TAILNET:
            raise ValueError("Excalibur needs an active Tailscale IPv4 address.")
        return cls(str(address), root / "tailnet-tls" / str(address))

    @property
    def certificate(self):
        return self.identity / "tls.crt"

    @property
    def key(self):
        return self.identity / "tls.key"

    def server_link(self):
        der = ssl.PEM_cert_to_DER_cert(self.certificate.read_text())
        pin = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).decode().rstrip("=")
        return f"nex://{self.address}:{CLIENT_PORT}/{pin}"

    def provision(self):
        self.identity.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.identity.parent / ".provision.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not (self.certificate.is_file() and self.key.is_file()):
                self._create_identity()

    def _create_identity(self):
        with tempfile.TemporaryDirectory(dir=self.identity, prefix=".tls-") as temp:
            stage = Path(temp)
            key, cert = stage / "tls.key", stage / "tls.crt"
            result = subprocess.run([
                "/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes",
                "-sha256", "-days", "397", "-keyout", str(key), "-out", str(cert),
                "-subj", f"/CN={self.address}", "-addext", f"subjectAltName=IP:{self.address}",
                "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "extendedKeyUsage=serverAuth",
                "-addext", "keyUsage=critical,digitalSignature",
            ], capture_output=True, text=True, timeout=20, check=False)
            if result.returncode:
                raise RuntimeError("Could not create the local Tailscale TLS certificate.")
            key.chmod(0o600)
            key.replace(self.key)
            cert.replace(self.certificate)
