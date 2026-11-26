"""Reuse the installed Piper voices, with the existing CLI available as a fallback."""

import json
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class WarmPiper:
    def __init__(self, engines):
        self.engines = engines
        self.lock = threading.Lock()
        self.process = self.profile = self.directory = None

    def close(self):
        with self.lock:
            self.stop()

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.directory:
            self.directory.cleanup()
        self.process = self.profile = self.directory = None

    def ensure(self, config):
        profile = tuple(config[key] for key in ("piper", "voice_en", "voice_tr"))
        if self.profile == profile and self.process and self.process.poll() is None:
            return Path(self.directory.name) / "piper.sock"
        self.stop()
        installed = self.engines.settings.resolve(profile[0])
        if not installed or not any(profile[1:]) or any(
            value and not self.engines.settings.resolve(value) for value in profile[1:]
        ):
            return None
        site = Path(installed).parent / "python"
        if not (site / "piper" / "__init__.py").is_file():
            return None
        local_site = site.relative_to(self.engines.settings.assets.resolve()).as_posix()
        self.directory = tempfile.TemporaryDirectory(prefix="piper-", dir=self.engines.runtime)
        work = Path(self.directory.name)
        endpoint = work / "piper.sock"
        host = Path(__file__).with_name("piper_host.py").resolve()
        try:
            launch = self.engines.sandbox(work) + [
                "--ro-bind", str(host), "/nex/piper_host.py",
                "--setenv", "PYTHONPATH", "/nex/assets/" + local_site,
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--", "/usr/bin/python3", "/nex/piper_host.py",
                *(self.engines.asset(value) if value else "" for value in profile[1:]),
            ]
            self.process = subprocess.Popen(launch, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 8
            while self.process.poll() is None and time.monotonic() < deadline:
                if endpoint.is_socket():
                    self.profile = profile
                    return endpoint
                time.sleep(0.04)
        except (OSError, ValueError):
            pass
        self.stop()
        return None

    def warm(self, config):
        with self.lock:
            self.ensure(config)

    def render(self, config, text, language):
        with self.lock:
            endpoint = self.ensure(config)
            if endpoint is None:
                return None
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(8)
                    connection.connect(str(endpoint))
                    message = json.dumps({"text": text, "language": language}, ensure_ascii=False)
                    connection.sendall(message.encode() + b"\n")
                    with connection.makefile("rb") as response:
                        header = response.read(4)
                        if len(header) == 4:
                            length, = struct.unpack("!I", header)
                            if 0 < length <= 12_000_000:
                                audio = response.read(length)
                                if len(audio) == length:
                                    return audio
            except OSError:
                self.stop()
            return None
