import http.client
import json
import math
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import wave
from array import array
from pathlib import Path

from .audio_files import open_audio
from .model_context import (
    INPUT_TOKENS_PATH,
    ContextBudget,
    context_size,
    input_tokens,
    log_completion,
    response_options,
)
from .settings import chat_route
from .transcription import has_audible_input, read_whisper_transcript
from .warm_speech import WarmPiper


class ModelError(Exception):
    pass


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Engines:
    def __init__(self, settings, runtime):
        self.settings = settings
        self.runtime = Path(runtime)
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.live_socket = self.runtime.parent / "nex-live" / "daybook.sock"
        self.lock = threading.Lock()
        self.process = self.profile = None
        self.model_dir = self.runtime / "llama"
        self.last_chat_error = None
        self.piper = WarmPiper(self)

    def sandbox(self, work, gpu=False):
        arguments = [
            "/usr/bin/bwrap", "--unshare-net", "--unshare-pid", "--unshare-ipc",
            "--new-session", "--die-with-parent", "--clearenv", "--setenv", "PATH", "/usr/bin",
            "--setenv", "HOME", "/tmp", "--setenv", "LC_ALL", "C.UTF-8",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin",
            "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--dir", "/nex", "--ro-bind", str(self.settings.assets.resolve()), "/nex/assets",
            "--bind", str(work), "/nex/work", "--chdir", "/nex/work",
        ]
        if gpu:
            for node in Path("/dev").glob("nvidia*"):
                arguments.extend(["--dev-bind", str(node), str(node)])
        return arguments

    def asset(self, value):
        path = self.settings.resolve(value)
        if not path:
            raise ModelError("The configured model or runtime file is missing.")
        return "/nex/assets/" + Path(path).relative_to(self.settings.assets.resolve()).as_posix()

    def run(self, work, args, timeout, error, input_bytes=None):
        try:
            subprocess.run(
                self.sandbox(work) + ["--", *args], check=True, timeout=timeout,
                input=input_bytes, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ModelError(error) from exc

    def decode(self, path, work, config):
        with open_audio(path) as source, (work / "input.m4a").open("wb") as target:
            shutil.copyfileobj(source, target)
        self.run(work, [
            self.asset(config["ffmpeg"]), "-nostdin", "-loglevel", "error", "-i", "input.m4a",
            "-t", "65", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "input.wav",
        ], 40, "The offline AAC decoder could not read this recording.")
        return work / "input.wav"

    def transcribe(self, path, language, config, *, wake=False, spoken=False):
        with tempfile.TemporaryDirectory(dir=self.runtime) as directory:
            work = Path(directory)
            decoded = self.decode(path, work, config)
            with wave.open(str(decoded), "rb") as stream:
                duration = stream.getnframes() / stream.getframerate()
                samples = array("h", stream.readframes(stream.getnframes()))
            if not has_audible_input(samples, spoken=spoken):
                return ""
            arguments = [
                self.asset(config["whisper_cli"]), "-m", self.asset(config["whisper_model"]),
                "-f", "input.wav", "-l", language, "-t", str(config["threads"]),
                "-ng", "-nt", "-sns", "-ojf", "-of", "transcript",
            ]
            if wake:
                arguments.extend(["--prompt", "Hey Nex. Merhaba Nex. Selam Nex."])
            if spoken and duration <= 20:
                # Whisper's encoder uses 50 audio positions per second.
                context = max(512, math.ceil(duration * 50) + 64)
                arguments.extend(["-ac", str(context), "-bo", "1", "-bs", "3"])
            self.run(work, arguments, 240, "Offline Whisper could not transcribe this recording.")
            try:
                return read_whisper_transcript(work / "transcript.json")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ModelError("Offline Whisper did not produce a transcript.") from exc

    def synthesize(self, text, language):
        config = self.settings.read()
        path = config["voice_" + language]
        if not config["piper"] or not path:
            return None
        audio = self.piper.render(config, text, language)
        if audio:
            return audio
        with tempfile.TemporaryDirectory(dir=self.runtime) as directory:
            work = Path(directory)
            self.run(work, [
                self.asset(config["piper"]), "--model", self.asset(path), "--output_file", "reply.wav",
            ], 40, "Offline Piper could not synthesize this reply.", (text + "\n").encode())
            output = work / "reply.wav"
            if not output.is_file() or output.stat().st_size > 12_000_000:
                raise ModelError("Offline Piper returned invalid audio.")
            return output.read_bytes()

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = self.profile = None

    def request(self, method, path, body=None, timeout=130, *, socket_path=None):
        conn = UnixHTTP(socket_path or self.model_dir / "model.sock", timeout)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json"})
            result = conn.getresponse()
            raw = result.read(200_000)
            if result.status != 200:
                return None
            return json.loads(raw)
        finally:
            conn.close()

    def stream_completion(self, options, socket_path, on_text):
        conn = UnixHTTP(socket_path or self.model_dir / "model.sock", 130)
        parts = []
        try:
            conn.request("POST", "/v1/chat/completions", json.dumps(options),
                         {"Content-Type": "application/json", "Accept": "text/event-stream"})
            with conn.getresponse() as response:
                if response.status != 200:
                    raise ModelError("The offline language model rejected the streamed request.")
                while line := response.readline(65_536):
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        return "".join(parts).strip()
                    choices = json.loads(payload).get("choices", [])
                    text = choices[0].get("delta", {}).get("content") if choices else None
                    if text:
                        parts.append(text)
                        on_text(text)
                raise ModelError("The offline language model stream ended early.")
        finally:
            conn.close()

    def ensure_model(self, config):
        keys = ("llama_server", "language_model", "threads", "gpu_layers", "context_size")
        profile = tuple(config[key] for key in keys)
        if self.profile == profile and self.process and self.process.poll() is None:
            return
        self.stop()
        self.model_dir.mkdir(mode=0o700, exist_ok=True)
        (self.model_dir / "model.sock").unlink(missing_ok=True)
        helper = Path(__file__).with_name("model_host.py").resolve()
        command = self.sandbox(self.model_dir, config["gpu_layers"] > 0) + [
            "--ro-bind", str(helper), "/nex/model_host.py", "--", "/usr/bin/python3", "/nex/model_host.py",
            self.asset(config["llama_server"]), "-m", self.asset(config["language_model"]),
            "--host", "127.0.0.1", "--port", "7787", "-t", str(config["threads"]),
            "-ngl", str(config["gpu_layers"]), "-c", str(config["context_size"]), "--parallel", "1",
        ]
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 65
            while self.process.poll() is None and time.monotonic() < deadline:
                try:
                    if self.request("GET", "/health", timeout=1):
                        self.profile = profile
                        return
                except (OSError, http.client.HTTPException, ValueError):
                    pass
                time.sleep(0.4)
        except OSError as exc:
            raise ModelError("Could not start the offline language runtime.") from exc
        self.stop()
        raise ModelError("The offline language model did not become ready. Check its files and runtime.")

    def mac_socket(self):
        path = self.runtime / "mac-model.sock"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        return path if info.st_uid == os.getuid() and stat.S_ISSOCK(info.st_mode) else None

    def model_status(self):
        result = self.settings.status()
        if self.managed_live():
            result["chat"] = "excalibur_live" if self.live_socket.is_socket() else "excalibur_unavailable"
        elif chat_route(self.settings.read()) == "mac":
            reachable = False
            try:
                path = self.mac_socket()
                reachable = bool(path and self.request("GET", "/health", timeout=0.5, socket_path=path))
            except (OSError, http.client.HTTPException, ValueError):
                pass
            result["chat"] = "mac_connected" if reachable else "mac_waiting"
        return result

    def managed_live(self):
        return (self.settings.path.parent / "live-model").is_file()

    def generate_daybook(self, messages, tokens):
        return self.generate_warm(messages, tokens) or self.generate(messages, tokens, self.settings.read())

    def classify_background(self, messages):
        if self.managed_live():
            return self.generate_warm(messages, 8, temperature=0)
        return self.classify(messages)

    def generate_background(self, messages, tokens):
        config = self.settings.read()
        if self.managed_live() or not chat_route(config):
            return self.generate_warm(messages, tokens, temperature=0)
        try:
            return self.generate(messages, tokens, config, wait=False, temperature=0, thinking=False)
        except ModelError:
            return None

    def classify(self, messages):
        config = self.settings.read()
        if not chat_route(config):
            return self.generate_warm(messages, 4, decision=True)
        try:
            return self.generate(messages, 4, config, wait=False, decision=True)
        except ModelError:
            return None

    def generate_warm(self, messages, tokens, *, decision=False, temperature=None):
        # The live daemon gives its active voice conversation priority over archive clients.
        body = {"messages": messages, "max_tokens": tokens} | response_options(messages)
        if decision:
            body["decision"] = True
        if temperature is not None:
            body["temperature"] = temperature
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5 if decision else 34)
                client.connect(str(self.live_socket))
                client.sendall(json.dumps(body, ensure_ascii=False).encode() + b"\n")
                with client.makefile("rb") as response:
                    result = json.loads(response.readline(32_768))
            if result.get("text"):
                return result["text"]
        except (OSError, http.client.HTTPException, ValueError):
            pass
        return None

    def budget_completion(self, options, config, socket_options):
        capacity = config["context_size"]
        if socket_options:
            props = self.request("GET", "/props", timeout=2, **socket_options)
            capacity = context_size(props, capacity)
        budget = ContextBudget(options, capacity)
        for candidate in budget.candidates():
            payload = self.request("POST", INPUT_TOKENS_PATH, candidate, timeout=2, **socket_options)
            if fitted := budget.fit(candidate, input_tokens(payload)):
                return fitted
        return budget.remaining()

    def generate(self, messages, tokens, config, *, on_text=None, wait=True, decision=False, temperature=None,
                 thinking=True):
        route = "live" if self.managed_live() else chat_route(config)
        if route is None:
            return None
        options = {"messages": messages, "max_tokens": tokens, "temperature": 0.25 if temperature is None else temperature,
                   "stream": on_text is not None} | response_options(messages)
        if decision or not thinking:
            options["chat_template_kwargs"] = {"enable_thinking": False}
        if decision:
            options["temperature"] = 0
        if not self.lock.acquire(blocking=wait):
            return None
        try:
            if route == "live":
                self.stop()
                answer = self.generate_warm(messages, tokens, decision=decision, temperature=temperature)
                self.last_chat_error = None if answer else "The Excalibur model did not answer."
                if answer and on_text:
                    on_text(answer)
                return answer
            if route == "server":
                self.ensure_model(config)
                path = None
            else:
                path = self.mac_socket()
                if not path:
                    raise ModelError("Start the Mac model relay to use the optional Mac backend.")
            socket_options = {"socket_path": path} if path is not None else {}
            started = time.monotonic()
            options = self.budget_completion(options, config, socket_options)
            if on_text is not None:
                answer = self.stream_completion(options, path, on_text)
                log_completion(route, started, None)
            else:
                output = self.request("POST", "/v1/chat/completions", options, timeout=5 if decision else 130, **socket_options)
                log_completion(route, started, output, status="ok" if output else "failed")
                answer = output["choices"][0]["message"]["content"].strip()
            if not answer:
                raise ModelError("The offline language model returned an empty answer.")
            self.last_chat_error = None
            return answer
        except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError) as exc:
            self.last_chat_error = ("The Mac model relay did not answer." if route == "mac"
                                    else "The offline language model request failed.")
            raise ModelError(self.last_chat_error) from exc
        except ModelError as exc:
            self.last_chat_error = str(exc)
            raise
        finally:
            self.lock.release()
