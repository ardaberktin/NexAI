import json
import os
import re
import threading
from pathlib import Path

from .model_context import DEFAULT_CONTEXT

DEFAULTS = {
    "llama_server": "", "language_model": "", "ffmpeg": "", "whisper_cli": "",
    "whisper_model": "", "piper": "", "voice_en": "", "voice_tr": "",
    "threads": 3, "gpu_layers": 0, "context_size": DEFAULT_CONTEXT, "use_mac_model": False,
}


def chat_route(config):
    if config["llama_server"]:
        return "server"
    if config.get("use_mac_model", False):
        return "mac"
    return None


class Settings:
    def __init__(self, data):
        self.path = Path(data) / "models.json"
        self.assets = Path(data) / "assets"
        self.assets.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.Lock()
        self.values = DEFAULTS | (json.loads(self.path.read_text()) if self.path.exists() else {})

    def read(self):
        with self.lock:
            return self.values.copy()

    def resolve(self, value):
        if not value:
            return None
        path = (self.assets / value).resolve()
        if not path.is_relative_to(self.assets.resolve()) or not path.is_file():
            return None
        return str(path)

    def update(self, values):
        if not isinstance(values, dict) or (values.keys() - DEFAULTS.keys()):
            raise ValueError("Unknown model setting.")
        with self.lock:
            result = self.values | values
            for key, default in DEFAULTS.items():
                value = result[key]
                if isinstance(default, bool):
                    if type(value) is not bool:
                        raise ValueError(f"{key} must be on or off.")
                elif isinstance(default, str):
                    if not isinstance(value, str) or len(value) > 240:
                        raise ValueError(f"Invalid file for {key}.")
                    if not value:
                        continue
                    if not re.fullmatch(r"[a-zA-Z0-9_.+/ -]+", value) or Path(value).is_absolute():
                        raise ValueError(f"Use a path relative to assets for {key}.")
                    path = self.resolve(value)
                    if not path:
                        raise ValueError(f"Missing asset for {key}: {value}")
                    if key in {"llama_server", "ffmpeg", "whisper_cli", "piper"} and not os.access(path, os.X_OK):
                        raise ValueError(f"The runtime for {key} must be executable.")
                else:
                    minimum, maximum = {"threads": (1, 6), "gpu_layers": (0, 99),
                                        "context_size": (2048, 8192)}[key]
                    if type(value) is not int or not minimum <= value <= maximum:
                        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
            for keys in (("llama_server", "language_model"), ("whisper_cli", "whisper_model")):
                if any(result[key] for key in keys) and not all(result[key] for key in keys):
                    raise ValueError(f"Configure {', '.join(keys)} together.")
            if result["whisper_cli"] and not result["ffmpeg"]:
                raise ValueError("Select a local AAC decoder before enabling Whisper.")
            if any(result[key] for key in ("voice_en", "voice_tr")) and not result["piper"]:
                raise ValueError("Configure Piper with the voice models.")
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump(result, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            self.values = result
            return self.values.copy()

    def status(self):
        current = self.read()
        return {
            "decoder": "configured" if self.resolve(current["ffmpeg"]) else "awaiting_decoder",
            "chat": "configured" if all(self.resolve(current[key]) for key in (
                "llama_server", "language_model"
            )) else "archive_search",
            "transcription": "configured" if all(self.resolve(current[key]) for key in (
                "whisper_cli", "whisper_model", "ffmpeg"
            )) else "awaiting_models",
            "speech": [language for language in ("en", "tr")
                       if self.resolve(current["piper"]) and self.resolve(current["voice_" + language])],
        }

    def files(self):
        files = []
        for root, _, names in os.walk(self.assets):
            for name in sorted(names):
                relative = Path(root, name).relative_to(self.assets).as_posix()
                if self.resolve(relative):
                    files.append(relative)
                if len(files) == 500:
                    return files
        return sorted(files)
