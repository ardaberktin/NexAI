"""Share the live speaker extractor with the archive worker over a private Unix socket."""

import asyncio
import socket
import tempfile
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from ..models import ModelError

MAX_BYTES = 22 * 16_000 * 2


@asynccontextmanager
async def speaker_socket(path, profiles):
    async def match(reader, writer):
        try:
            async with asyncio.timeout(12):
                count = int.from_bytes(await reader.readexactly(4), "big")
                if 0 < count <= MAX_BYTES and count % 2 == 0:
                    raw = await reader.readexactly(count)
                    speaker = await asyncio.to_thread(profiles.identify, raw)
                    writer.write(b"1" if speaker == "recognized" else b"0")
                    await writer.drain()
        except (ValueError, OSError, EOFError, TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(match, path=path)
    try:
        async with server:
            yield
    finally:
        path.unlink(missing_ok=True)


def recorded_speaker(store, engines, question, config):
    with store.privacy.transaction() as db:
        enrolled = db.execute("SELECT 1 FROM voices LIMIT 1").fetchone()
    if not enrolled or question.neighbor or question.target["duration"] > 22 or not config.get("ffmpeg"):
        return "unverified"
    path = engines.runtime.parent / "nex-live" / "speaker.sock"
    if not path.is_socket():
        return "unverified"
    try:
        with tempfile.TemporaryDirectory(dir=engines.runtime) as work:
            wav = engines.decode(store.audio_path(question.target["id"]), Path(work), config)
            with wave.open(str(wav), "rb") as audio:
                if audio.getnframes() * 2 > MAX_BYTES or (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2):
                    return "unverified"
                raw = audio.readframes(audio.getnframes())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(8)
            client.connect(str(path))
            client.sendall(len(raw).to_bytes(4, "big") + raw)
            return "recognized" if client.recv(1) == b"1" else "unverified"
    except (OSError, EOFError, ModelError, wave.Error):
        return "unverified"
