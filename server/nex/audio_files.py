"""Durable audio files with a stable archive path across both drives."""

import hashlib
import os
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile


def sync_directory(path):
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def open_audio(path):
    path = Path(path)
    try:
        return path.open("rb")
    except FileNotFoundError:
        # A reader can follow the old SSD link as the archive replaces it.
        return path.open("rb")


class AudioFiles:
    def __init__(self, directory, ssd=None):
        self.directory = directory
        self.ssd = Path(ssd).resolve() if ssd is not None else None
        if self.ssd is not None:
            self.ssd.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, identifier):
        if not identifier or identifier in (".", "..") or Path(identifier).name != identifier:
            raise ValueError("Invalid recording ID.")
        return self.directory / (identifier + ".m4a")

    @staticmethod
    def replace_file(target, write):
        with NamedTemporaryFile(dir=target.parent, prefix=".audio-", delete=False) as stream:
            staging = Path(stream.name)
            try:
                write(stream)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(staging, target)
                sync_directory(target.parent)
            finally:
                staging.unlink(missing_ok=True)

    def write(self, identifier, audio, on_ssd):
        canonical = self.path(identifier)
        if not on_ssd:
            self.replace_file(canonical, lambda stream: stream.write(audio))
            if self.ssd is not None:
                self.remove_ssd(canonical.name)
            return
        target = self.ssd / canonical.name
        self.replace_file(target, lambda stream: stream.write(audio))
        # The previous server release can still follow this archive path.
        staging = canonical.with_suffix(".link")
        try:
            staging.unlink(missing_ok=True)
            staging.symlink_to(target)
            os.replace(staging, canonical)
            sync_directory(self.directory)
        finally:
            staging.unlink(missing_ok=True)

    def remove_ssd(self, name):
        try:
            (self.ssd / name).unlink()
        except FileNotFoundError:
            pass
        else:
            sync_directory(self.ssd)

    def remove(self, identifier):
        canonical = self.path(identifier)
        canonical.unlink(missing_ok=True)
        if self.ssd is not None:
            self.remove_ssd(canonical.name)

    def offload(self, identifier, expected_sha):
        canonical = self.path(identifier)
        if canonical.is_symlink() or not canonical.is_file():
            with (self.ssd / canonical.name).open("rb") as source:
                self.replace_file(canonical, lambda target: self.copy_verified(source, target, expected_sha))
        else:
            # A restart can occur after replacing the link but before updating SQLite.
            with canonical.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != expected_sha:
                    raise OSError("The data drive copy did not match the archived recording.")
                os.fsync(source.fileno())
            sync_directory(self.directory)
        self.remove_ssd(canonical.name)

    @staticmethod
    def copy_verified(source, target, expected_sha):
        copyfileobj(source, target)
        target.flush()
        with open(target.name, "rb") as written:
            if hashlib.file_digest(written, "sha256").hexdigest() != expected_sha:
                raise OSError("The copied audio did not match the archived recording.")
