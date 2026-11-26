"""Run the installed Piper voices inside Nex's offline model sandbox."""

import io
import json
import socketserver
import struct
import sys
import wave
from pathlib import Path

import onnxruntime
from piper import PiperVoice
from piper.config import PiperConfig


def load_voice(path):
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    config = json.loads(Path(path + ".json").read_text(encoding="utf-8"))
    return PiperVoice(
        config=PiperConfig.from_dict(config),
        session=onnxruntime.InferenceSession(path, sess_options=options, providers=["CPUExecutionProvider"]),
    )


class Render(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(8)
        try:
            request = json.loads(self.rfile.readline(16_384))
            text, language = request["text"], request["language"]
            if not isinstance(text, str) or not 1 <= len(text) <= 1800:
                raise ValueError("Invalid speech text.")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                self.server.voices[language].synthesize_wav(text, wav)
            audio = output.getvalue()
        except Exception:  # noqa: BLE001 - an empty frame tells the caller to try its fallback voice.
            audio = b""
        self.wfile.write(struct.pack("!I", len(audio)))
        self.wfile.write(audio)


if __name__ == "__main__":
    voices = {}
    for language, path in zip(("en", "tr"), sys.argv[1:], strict=True):
        if path:
            voice = load_voice(path)
            with wave.open(io.BytesIO(), "wb") as sample:
                voice.synthesize_wav("Hello." if language == "en" else "Merhaba.", sample)
            voices[language] = voice
    with socketserver.UnixStreamServer("/nex/work/piper.sock", Render) as server:
        server.voices = voices
        server.serve_forever()
