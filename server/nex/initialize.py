"""Create the private archive and initial access keys for a self-hosted server."""

import fcntl
import json
import os
import secrets
import tempfile


def initialize(root, quota_gib, reserve_gib):
    if not 0 < quota_gib <= 2**33 - 1 or not 0 <= reserve_gib <= 2**33 - 1:
        raise ValueError("Use a positive audio quota and a nonnegative disk reserve in GiB.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("The archive directory must be accessible only to its owner (chmod 700).")
    (root / "assets").mkdir(mode=0o700, exist_ok=True)
    with (root / ".init.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        access = root / "access.json"
        if access.is_symlink():
            raise ValueError("Use a regular access.json file inside the archive.")
        if access.exists():
            if not access.is_file() or access.stat().st_mode & 0o077:
                raise ValueError("The access.json file must be accessible only to its owner (chmod 600).")
            return
        settings = {
            "keys": {role: secrets.token_urlsafe(36) for role in ("admin", "mac", "iphone")},
            "audio_quota_bytes": quota_gib * 2**30,
            "min_free_bytes": reserve_gib * 2**30,
        }
        with tempfile.TemporaryDirectory(dir=root, prefix=".init-") as staging:
            path = os.path.join(staging, "access.json")
            with open(path, "x", opener=lambda file, flags: os.open(file, flags, 0o600)) as stream:
                json.dump(settings, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, access)
