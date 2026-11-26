"""Send privacy-preserving goal alerts through Apple's HTTP/2 provider API."""

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from time import time

TOPIC = "com.ardaberktin.nex"
HOSTS = {"development": "api.sandbox.push.apple.com", "production": "api.push.apple.com"}
KEY_ID = re.compile(r"[A-Z0-9]{10}")
TOKEN = re.compile(r"(?:[a-f0-9]{2}){1,128}")


def base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def wire(value):
    return json.dumps(value, separators=(",", ":"))


@dataclass
class Provider:
    environment: str
    team_id: str
    key_id: str
    key: object
    bearer: str = ""
    issued: int = 0

    @classmethod
    def load(cls, root):
        directory = root / "apns"
        try:
            config_file, key_file = directory / "config.json", directory / "key.p8"
            if config_file.stat().st_mode & 0o077 or key_file.stat().st_mode & 0o077:
                return None
            config = json.loads(config_file.read_text())
            environment, team, kid = (config[name] for name in ("environment", "team_id", "key_id"))
            if environment not in HOSTS or not KEY_ID.fullmatch(team) or not KEY_ID.fullmatch(kid):
                return None
        except (OSError, ValueError, TypeError, KeyError):
            return None
        try:
            from cryptography.exceptions import UnsupportedAlgorithm
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec

        except ImportError:
            return None
        try:
            key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                return None
            return cls(environment, team, kid, key)
        except (OSError, UnsupportedAlgorithm, ValueError, TypeError):
            return None

    def authorization(self):
        now = int(time())
        if self.bearer and 0 <= now - self.issued < 45 * 60:
            return self.bearer
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        header = base64url(wire({"alg": "ES256", "kid": self.key_id}).encode())
        claims = base64url(wire({"iss": self.team_id, "iat": now}).encode())
        message = header + "." + claims
        der = self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        self.bearer = message + "." + base64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        self.issued = now
        return self.bearer

    def send(self, token, identifier, expires):
        if not TOKEN.fullmatch(token):
            return 400, "BadDeviceToken"
        payload = wire({"aps": {"alert": {"title": "Nex", "body": "A goal update is ready."}, "sound": "default"},
                        "nex_event": identifier})
        # curl reads the signed provider token from stdin, so it is absent from process arguments.
        options = [
            ("url", f"https://{HOSTS[self.environment]}/3/device/{token}"),
            ("header", "authorization: bearer " + self.authorization()),
            ("header", "apns-topic: " + TOPIC),
            ("header", "apns-push-type: alert"),
            ("header", "apns-id: " + identifier),
            ("header", "apns-collapse-id: " + identifier),
            ("header", f"apns-expiration: {expires}"),
            ("header", "content-type: application/json"),
            ("data-binary", payload),
        ]
        config = "\n".join(f"{option} = {json.dumps(value)}" for option, value in options)
        try:
            response = subprocess.run(
                ["/usr/bin/curl", "--disable", "--silent", "--http2", "--proto", "=https", "--tlsv1.2",
                 "--noproxy", "*", "--max-time", "10", "--max-filesize", "8192", "--write-out", "\n%{http_code}",
                 "--config", "-"],
                input=config, text=True, capture_output=True, timeout=12, check=False,
            )
            if response.returncode:
                return 503, "Transport"
            body, _, status = response.stdout.rpartition("\n")
            reason = json.loads(body).get("reason", "") if body else ""
            return int(status), reason
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            return 503, "Transport"
