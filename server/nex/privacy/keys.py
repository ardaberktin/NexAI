"""Bind each paired app to a biometric-protected P-256 signing key."""

import base64
import hmac
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from time import time

from .state import AccessDenied, approval_subject, digest

# SubjectPublicKeyInfo for an uncompressed point on prime256v1.
P256_SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
SESSION_SECONDS = 12 * 3600


def issue_setup(state, device):
    secret = secrets.token_urlsafe(32)
    with state.transaction(write=True) as db:
        db.execute("INSERT INTO keys(device,bootstrap) VALUES(?,?) ON CONFLICT(device) "
                   "DO UPDATE SET bootstrap=excluded.bootstrap", (device, digest(secret)))
    return secret


def verify(public, message, signature):
    try:
        sig = base64.b64decode(signature, validate=True)
        if len(public) != 65 or public[0] != 4 or not 8 <= len(sig) <= 72:
            return False
        with tempfile.TemporaryDirectory() as directory:
            key, signed = Path(directory, "public.pem"), Path(directory, "signature.der")
            key.write_bytes(b"-----BEGIN PUBLIC KEY-----\n" + base64.encodebytes(P256_SPKI + public)
                            + b"-----END PUBLIC KEY-----\n")
            signed.write_bytes(sig)
            result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(key), "-signature", str(signed)],
                                    input=message.encode(), capture_output=True, timeout=3, check=False)
            return result.returncode == 0
    except (ValueError, TypeError, OSError, subprocess.TimeoutExpired):
        return False


def signing_device(device, client):
    return f"{device}:{client.id}" if client else device


def challenge(state, device, purpose, subject, *, client=None):
    if purpose not in {"unlock", "approve", "voice"} or not isinstance(subject, str) or len(subject) > 128:
        raise ValueError("Invalid biometric request.")
    if purpose == "approve" and device != "iphone":
        raise AccessDenied("Approve private requests with Face ID on the paired iPhone.")
    if ((purpose == "unlock" and subject)
            or (purpose == "voice" and not re.fullmatch(r"delete|enroll-[a-f0-9]{64}", subject))):
        raise ValueError("Invalid biometric operation.")
    device = signing_device(device, client)
    nonce = secrets.token_urlsafe(32)
    now = time()
    with state.transaction(write=True) as db:
        db.execute("DELETE FROM challenges WHERE expires<=?", (now,))
        if purpose == "approve":
            identifier = subject.partition(":")[0]
            row = db.execute("SELECT * FROM requests WHERE id=?", (identifier,)).fetchone()
            if (not row or row["state"] != "pending" or row["created"] < now - 600
                    or subject != approval_subject(row)):
                raise AccessDenied("This request is no longer awaiting approval.")
        if db.execute("SELECT count(*) FROM challenges WHERE device=?", (device,)).fetchone()[0] >= 32:
            raise AccessDenied("Too many outstanding biometric requests. Try again shortly.")
        db.execute("INSERT INTO challenges VALUES(?,?,?,?,?)", (nonce, device, purpose, subject, now + 90))
    return {"nonce": nonce, "message": signing_message(nonce, device, purpose, subject)}


def signing_message(nonce, device, purpose, subject):
    return f"Nex biometric approval v1\n{device}\n{purpose}\n{subject}\n{nonce}"


def consume(db, device, purpose, subject, proof, *, client=None):
    device = signing_device(device, client)
    nonce = proof.get("nonce")
    if not isinstance(nonce, str) or len(nonce) > 64:
        raise AccessDenied("A fresh biometric challenge is required.")
    row = db.execute("SELECT expires FROM challenges WHERE id=? AND device=? AND purpose=? AND subject=?",
                     (nonce, device, purpose, subject)).fetchone()
    if client:
        if not row or row[0] <= time() or not verify(
            client.public, signing_message(nonce, device, purpose, subject), proof.get("signature")
        ):
            raise AccessDenied("Biometric approval could not be verified.")
        db.execute("DELETE FROM challenges WHERE id=?", (nonce,))
        return
    key = db.execute("SELECT public,bootstrap FROM keys WHERE device=?", (device,)).fetchone()
    if not row or row[0] <= time() or not key:
        raise AccessDenied("Import a current Nex pairing profile or request a fresh biometric approval.")
    public = key["public"]
    if purpose == "unlock":
        candidate = base64.b64decode(proof.get("public", ""), validate=True)
        if candidate != public:
            setup = proof.get("setup") or ""
            if not isinstance(setup, str) or not key["bootstrap"] or not hmac.compare_digest(key["bootstrap"], digest(setup)):
                raise AccessDenied("Import a newly exported pairing profile to register this device key.")
            public = candidate
    if not public or not verify(public, signing_message(nonce, device, purpose, subject), proof.get("signature")):
        raise AccessDenied("Biometric approval could not be verified.")
    db.execute("DELETE FROM challenges WHERE id=?", (nonce,))
    if public != key["public"]:
        db.execute("UPDATE keys SET public=?,bootstrap=NULL WHERE device=?", (public, device))
        db.execute("DELETE FROM sessions WHERE device=?", (device,))
        db.execute("DELETE FROM challenges WHERE device=?", (device,))


def unlock(state, device, proof, *, client=None):
    token = secrets.token_urlsafe(32)
    with state.transaction(write=True) as db:
        consume(db, device, "unlock", "", proof, client=client)
        db.execute("DELETE FROM sessions WHERE device=? OR expires<=?", (device, time()))
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), device, time() + SESSION_SECONDS))
        if client:
            db.execute("INSERT INTO session_devices VALUES(?,?)", (digest(token), client.id))
    return {"token": token}


def require_session(state, device, token, *, client=None):
    with state.transaction() as db:
        validate_session(db, device, token)
        binding = db.execute("SELECT device_id FROM session_devices WHERE token=?", (digest(token),)).fetchone()
        if (binding[0] if binding else None) != (client.id if client else None):
            raise AccessDenied("Unlock the Nex workspace with biometrics.")


def validate_session(db, device, token):
    found = db.execute("SELECT 1 FROM sessions WHERE token=? AND device=? AND expires>?",
                       (digest(token), device, time())).fetchone()
    if not found:
        raise AccessDenied("Unlock the Nex workspace with biometrics.")
