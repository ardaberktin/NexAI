import base64
import hmac
import json
import logging
import math
import re
import socket
import ssl
import threading
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .accounts.credentials import Credentials
from .accounts.http import AccountError
from .activities.current import current_snapshot
from .activities.jobs import record_screen
from .activities.recall import activity_evidence, activity_question
from .activities.timeline import timeline
from .audio_files import open_audio
from .coding.intent import pasted_python
from .coding.sandbox import MAX_SOURCE
from .daybook import Daybook, DayContext
from .hosted import routes as account_routes
from .hosted.auth import BadLogin, LoginBusy, ResetInProgress, bearer
from .hosted.setup import claim_owner
from .models import ModelError
from .personal_files import PersonalConflict, safe_line
from .privacy import public, routes
from .privacy.keys import require_session
from .privacy.state import AccessDenied
from .recall_dates import time_zone
from .store import ArchiveFull, RecordingConflict, TranscriptConflict, now
from .voice_chime import CHIME_WAV
from .web_answers import follow_link
from .web_transport import WebReadError
from .workspace_features import FeatureDisabled, is_enabled, require_enabled
from .workspace_http import workspace_request
from .workspace_schema import WorkspaceConflict, WorkspaceMissing

UUID = r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}"
RECORDING = re.compile(r"/v1/recordings/(" + UUID + r")(?:/(audio|transcript|correction|retry|voice-stream|receipt))?")
WATCH_RECORDING = re.compile(r"/v2/watch/recordings/(" + UUID + r")")
WATCH_RECEIPT = re.compile(r"/v2/watch/recordings/(" + UUID + r")/receipt")
SOURCE = re.compile(r"/v1/memory/((?:[anqg]-)?" + UUID + r")")
WEB = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/nex.js": ("nex.js", "text/javascript; charset=utf-8"),
    "/nex.css": ("nex.css", "text/css; charset=utf-8"),
}
logger = logging.getLogger(__name__)


WEB_DIR = Path(__file__).with_name("web")


def safe_voice_reply(reply):
    return reply and (reply.get("public_voice") is True or reply.get("engine") == "private_request")


def recorder_receipt(row):
    receipt = {key: row[key] for key in ("id", "started", "mode", "state", "error")}
    receipt["reply"] = row["reply"] if safe_voice_reply(row.get("reply")) else None
    return receipt


def recorder_operation(method, path):
    if (method, path) in {("GET", "/v1/status"), ("GET", "/v1/replies"), ("POST", "/v1/gaps"),
                          ("GET", "/v1/workspace/goal-alerts"),
                          ("GET", "/v1/workspace/clock-alerts"),
                          ("PUT", "/v1/workspace/clock-alerts"),
                          ("PUT", "/v1/workspace/iphone-recording"), ("PUT", "/v1/workspace/recording-status"),
                          ("POST", "/v1/speech"),
                          ("PUT", "/v1/workspace/weather-location/fix"),
                          ("PUT", "/v1/workspace/weather-location/clear")}:
        return True
    match = RECORDING.fullmatch(path)
    return bool(match and ((method == "PUT" and match[2] in {None, "transcript"})
                           or (method == "GET" and match[2] in {"receipt", "voice-stream"})))


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or not 2000 <= dt.year <= 2100:
        raise ValueError("Use a timestamp with a time zone.")
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def valid_language(value):
    if value not in ("auto", "en", "tr"):
        raise ValueError("Choose auto, en, or tr.")
    return value


def chat_message(data):
    value = data.get("text")
    text = value.strip() if isinstance(value, str) else ""
    if 0 < len(text) <= 2500:
        return text
    if (data.get("daybook_day") is None and 2500 < len(text) <= MAX_SOURCE + 256
            and (program := pasted_python(text)) and len(program.source.encode()) <= MAX_SOURCE):
        return text
    raise ValueError("Enter a question up to 2,500 characters or a Python script up to 12,000 bytes.")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 24
    handler = None

    def __init__(self, address, app, *, hosted, tls=None, admin=False, allow_peer=None, interface=None):
        if not admin and tls is None:
            raise ValueError("Hosted account sign-in requires TLS.")
        self.app, self.tls, self.admin = app, tls, admin
        self.hosted = hosted
        self.allow_peer, self.interface = allow_peer, interface
        self.slots = threading.BoundedSemaphore(22)
        super().__init__(address, self.handler or API)

    def server_bind(self):
        if self.interface:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode() + b"\0")
        super().server_bind()

    def verify_request(self, request, client_address):
        return self.allow_peer is None or self.allow_peer(client_address[0])

    def process_request(self, request, client_address):
        if self.slots.acquire(blocking=False):
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(12)
            if self.tls:
                try:
                    request = self.tls.wrap_socket(request, server_side=True)
                except (ssl.SSLError, OSError):
                    self.shutdown_request(request)
                    return
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class API(BaseHTTPRequestHandler):
    server_version = "Nex"
    sys_version = ""
    workspace = client_identity = None

    def log_message(self, *_):
        pass

    @property
    def app(self):
        return self.workspace or self.server.app

    @property
    def privacy(self):
        return self.app.privacy

    @property
    def chat_followups(self):
        return self.app.chat_followups

    @property
    def recording_status(self):
        return self.app.recording_status

    def do_GET(self):
        self.run("GET")

    def do_POST(self):
        self.run("POST")

    def do_PUT(self):
        self.run("PUT")

    def send(self, body, status=200, mime="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "media-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def run(self, method):
        self.workspace = self.client_identity = None
        try:
            url = urlsplit(self.path)
            if url.scheme or url.netloc or url.fragment:
                raise Problem(400, "Invalid request target.")
            if self.server.admin and self.headers.get("Host") not in (
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}",
            ):
                raise Problem(403, "Use the localhost address for the console.")
            if method == "GET" and url.path in WEB and self.server.admin:
                name, content = WEB[url.path]
                self.send((WEB_DIR / name).read_bytes(), mime=content)
                return
            token = bearer(self.headers.get("Authorization", ""))
            if self.server.admin:
                if not hmac.compare_digest(token.encode(), self.app.admin_key.encode()):
                    raise Problem(401, "Enter the console access key.")
                device = "console"
            else:
                device = self.authorize_client(method, url.path, token)
                if device is None:
                    return
            query = {key: values[-1] for key, values in parse_qs(
                url.query, keep_blank_values=True, max_num_fields=12
            ).items()}
            self.dispatch(method, url.path, query, device)
        except BadLogin:
            self.send({"error": "The username or password could not be verified."}, 401)
        except LoginBusy:
            self.send({"error": "Too many account requests. Try again shortly."}, 429)
        except ResetInProgress:
            self.send({"error": "Nex is resetting this account’s data. Try signing in again shortly."}, 503)
        except Problem as exc:
            self.send({"error": exc.message}, exc.status)
        except AccessDenied as exc:
            self.send({"error": str(exc)}, 403)
        except FeatureDisabled as exc:
            self.send({"error": str(exc)}, 403)
        except (TypeError, ValueError, OverflowError) as exc:
            self.send({"error": str(exc)}, 400)
        except ArchiveFull:
            self.send({"error": "Excalibur's Nex storage allowance is full. The device retains this clip."}, 507)
        except RecordingConflict:
            self.send({"error": "This recording ID is already used by another upload."}, 409)
        except TranscriptConflict as exc:
            self.send({"error": str(exc)}, 409)
        except (TimeoutError, BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        except Exception as exc:  # noqa: BLE001 - keep unexpected failures inside the request boundary.
            logger.error("Request failed (%s)", type(exc).__name__)
            self.send({"error": "Nex could not complete the request."}, 500)

    def authorize_client(self, method, path, token):
        hosted = self.server.hosted
        accounts = hosted.accounts
        peer = self.client_address[0]
        if (method, path) == ("POST", "/v2/auth/login"):
            self.send(accounts.sign_in(self.object(8_000), peer))
            return None
        if (method, path) == ("POST", "/v2/auth/setup"):
            self.send(claim_owner(accounts, self.object(8_000), peer))
            return None
        client = accounts.authenticate(token)
        if client is None:
            raise Problem(401, "Sign in to this Nex server.")
        if (method, path) == ("POST", "/v2/auth/logout"):
            accounts.revoke(token)
            self.send({"ok": True})
            return None
        self.client_identity = client
        self.workspace = hosted.workspace(client.account)
        if path.startswith("/v2/auth/"):
            self.send(account_routes.account_request(self, hosted, method, path, Problem))
            return None
        return client.role

    def read(self, limit):
        try:
            count = int(self.headers["Content-Length"])
        except (ValueError, TypeError):
            raise Problem(411, "A content length is required.") from None
        if not 0 < count <= limit:
            raise Problem(413, "Request exceeds the supported size.")
        data = self.rfile.read(count)
        if len(data) != count:
            raise Problem(400, "Incomplete upload.")
        return data

    def object(self, limit):
        if self.headers.get_content_type() != "application/json":
            raise Problem(415, "Use application/json.")
        obj = json.loads(self.read(limit))
        if not isinstance(obj, dict):
            raise Problem(400, "Send a JSON object.")
        return obj

    def dispatch(self, method, path, query, device):
        if method == "GET" and device == "iphone" and (match := WATCH_RECEIPT.fullmatch(path)):
            row = self.app.store.recording(match[1])
            if not row or row["device"] != "watch":
                raise Problem(404, "Watch recording not found.")
            self.send(recorder_receipt(row))
            return
        if method == "PUT" and device == "iphone" and (match := WATCH_RECORDING.fullmatch(path)):
            self.upload(match[1], "watch")
            return
        if path.startswith("/v2/"):
            path = "/v1/" + path.removeprefix("/v2/")
        store = self.app.store
        if path.startswith("/v1/privacy/") and device != "console":
            self.send(routes.route(self, method, path.removeprefix("/v1/privacy/"), device, Problem))
            return
        if device != "console" and not recorder_operation(method, path):
            require_session(self.privacy, self.client_identity, self.headers.get("X-Nex-Workspace", ""))
        if path.startswith("/v1/workspace/"):
            try:
                result = workspace_request(self, method, path, query, device, Problem)
                if isinstance(result, bytes):
                    self.send(result, mime="application/pdf")
                else:
                    self.send(result)
            except WorkspaceConflict as exc:
                raise Problem(409, str(exc)) from exc
            except WorkspaceMissing as exc:
                raise Problem(404, str(exc)) from exc
            return
        if (method, path) == ("POST", "/v1/web/read"):
            self.web_read()
            return
        if path in {"/v1/context", "/v1/context/screen"}:
            self.context_request(method, path, device, query)
            return
        if path in {"/v1/accounts/setup", "/v1/accounts/start", "/v1/accounts/finish", "/v1/accounts/disconnect"}:
            self.account_request(method, path, device)
            return
        if method == "GET":
            if path == "/v1/activities":
                require_enabled(store, "activities")
                if query.get("current") == "1":
                    self.send(current_snapshot(store))
                    return
                context = DayContext.parse(query.get("day"), query.get("time_zone", "UTC"), query.get("source"))
                self.send(timeline(store, context.bounds, source=context.source))
                return
            if path == "/v1/status":
                status = store.status() | {"models": self.app.engines.model_status()}
                if device == "console" or self.headers.get("X-Nex-Workspace"):
                    if device != "console":
                        require_session(self.privacy, self.client_identity, self.headers["X-Nex-Workspace"])
                    status["connections"] = Credentials(store.root).status()
                self.send(status)
                return
            if path in {"/v1/daybook", "/v1/daybook/conversation"}:
                require_enabled(store, "daybook")
                anchor = query.get("source") if path.endswith("/conversation") else None
                context = DayContext.parse(query.get("day"), query.get("time_zone", "UTC"), anchor)
                daybook = Daybook(store)
                if path.endswith("/conversation"):
                    conversation = daybook.conversation(context)
                    if conversation is None:
                        raise Problem(404, "That conversation is no longer available for this day.")
                    self.send(conversation)
                else:
                    self.send(daybook.overview(context))
                return
            if path == "/v1/memory":
                value = query.get("q", "").strip()
                day = date.fromisoformat(query["day"]).isoformat() if query.get("day") else None
                self.send({"items": store.search(value[:600], day=day)})
                return
            if match := SOURCE.fullmatch(path):
                entry = store.source(match[1])
                if entry is None:
                    raise Problem(404, "Memory was not found.")
                self.send(entry)
                return
            if path == "/v1/recordings":
                cursor = query.get("before", "")
                before = ("9999", "")
                if cursor:
                    start, identifier = cursor.split(",", 1)
                    if not re.fullmatch(UUID, identifier):
                        raise ValueError("Invalid timeline cursor.")
                    before = (timestamp(start), identifier)
                self.send({"items": store.recordings(before)})
                return
            if path == "/v1/replies" and device != "console":
                since = timestamp(query.get("since", now()))
                self.send({"items": [recorder_receipt(row) for row in store.ready_replies(device, since)
                                     if safe_voice_reply(row.get("reply"))]})
                return
        if match := RECORDING.fullmatch(path):
            self.handle_recording(method, match[1], match[2], device)
            return
        if method == "POST" and path in {"/v1/chat", "/v1/web/chat"}:
            self.chat_request(device, lookup_only=path == "/v1/web/chat")
            return
        if (method, path) == ("POST", "/v1/speech"):
            self.speech()
            return
        if (method, path) == ("POST", "/v1/gaps"):
            self.record_gap(device)
            return
        if self.server.admin and path == "/v1/admin/models":
            self.admin_models(method)
            return
        if self.server.admin and path == "/v1/admin/storage":
            if method == "PUT":
                self.send(store.archive.configure(self.object(2_000)))
            elif method == "GET":
                self.send(store.archive.settings())
            else:
                raise Problem(405, "Use GET or PUT to manage storage.")
            return
        raise Problem(404, "This Nex endpoint was not found.")

    def account_request(self, method, path, device):
        if device == "console":
            raise Problem(403, "Manage connected accounts in a paired Nex app.")
        sign_in = self.app.sign_in
        try:
            if (method, path) == ("GET", "/v1/accounts/setup"):
                result = sign_in.options(device)
            elif (method, path) == ("PUT", "/v1/accounts/setup"):
                result = sign_in.set_google_client(self.object(3000), device)
            elif (method, path) == ("POST", "/v1/accounts/start"):
                body = self.object(1000)
                result = sign_in.start(body.get("provider"), body.get("port"), device)
            elif (method, path) == ("POST", "/v1/accounts/finish"):
                body = self.object(12000)
                result = sign_in.finish(body.get("provider"), body.get("callback"), device)
            elif (method, path) == ("POST", "/v1/accounts/disconnect"):
                result = sign_in.disconnect(self.object(1000).get("provider"))
            else:
                raise Problem(405, "This connected account operation is unavailable.")
        except AccountError as error:
            raise Problem(422, str(error)) from None
        self.send(result)

    def chat_request(self, device, *, lookup_only):
        data = self.object(80_000)  # Includes JSON escaping for pasted programs.
        message, identifier = chat_message(data), data.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(UUID, identifier):
            raise ValueError("Invalid question ID.")
        zone = time_zone(data.get("time_zone", "UTC")).key
        language = valid_language(data.get("language", "auto"))
        day = data.get("daybook_day")
        if day is not None:
            require_enabled(self.app.store, "daybook")
        journal = DayContext.parse(day, zone, data.get("daybook_source"), data.get("daybook_focus")) if day is not None else None
        if not self.app.chat_slot.acquire(blocking=False):
            raise Problem(429, "Nex is answering another question. Try again shortly.")
        try:
            self.send(public.chat(self.app.assistant, identifier, device, message, language, zone,
                                  journal=journal, force=data.get("access") == "private", lookup_only=lookup_only,
                                  followups=self.chat_followups, location=data.get("weather_location"),
                                  ask_location=data.get("weather_location_supported") is True,
                                  device_clock=data.get("device_clock"),
                                  session=self.headers.get("X-Nex-Workspace") if device != "console" else None))
        finally:
            self.app.chat_slot.release()

    def web_read(self):
        data = self.object(2_000)
        source, index = data.get("source"), data.get("link")
        if not isinstance(source, str) or not re.fullmatch(UUID, source) or (index is not None and type(index) is not int):
            raise ValueError("Choose a link from a web source.")
        try:
            result = follow_link(self.app.store, source, index)
        except WebReadError as exc:
            raise Problem(422, str(exc)) from None
        if not result:
            raise Problem(404, "That web link is no longer available.")
        self.send(result)

    def context_request(self, method, path, device, query):
        files = self.app.assistant.personal
        if path == "/v1/context" and method == "GET":
            snapshot = files.snapshot()
            if (question := query.get("question", "")[:2500]) and is_enabled(self.app.store, "activities"):
                zone = time_zone(query.get("time_zone", "UTC")).key
                snapshot["activity_request"] = activity_question(question)
                snapshot["activities"] = activity_evidence(self.app.store, question, zone)
            self.send(snapshot)
        elif path == "/v1/context" and method == "PUT":
            body = self.object(100_000)
            try:
                self.send(files.edit(body.get("name"), body.get("revision"), body.get("markdown")))
            except PersonalConflict as exc:
                raise Problem(409, str(exc)) from None
        elif path == "/v1/context/screen" and method == "PUT":
            if device != "mac":
                raise Problem(403, "Only your signed-in Mac can report screen activity.")
            body = self.object(4_000)
            observed = datetime.fromisoformat(timestamp(body.get("observed")))
            summary = body.get("summary")
            if summary is not None and (not isinstance(summary, str) or len(summary) > 480):
                raise ValueError("Use a short screen activity description.")
            files.signal("Mac screen", summary or "Screen sharing is paused or stopped.", observed)
            record_screen(self.app.store, observed, safe_line(summary, 360) or None)
            self.send({"ok": True})
        else:
            raise Problem(405, "Use GET or PUT for personal context.")

    def handle_recording(self, method, identifier, action, device):
        store = self.app.store
        if method == "PUT" and action is None and device != "console":
            self.upload(identifier, device)
            return
        if action == "correction":
            self.handle_correction(method, identifier, device)
            return
        row = store.recording(identifier)
        if row is None:
            raise Problem(404, "Recording was not found.")
        if method == "GET" and action == "receipt" and device == row["device"]:
            self.send(recorder_receipt(row))
        elif method == "GET" and action is None:
            self.send(row)
        elif method == "GET" and action == "voice-stream" and device == row["device"]:
            self.voice_stream(identifier)
        elif method == "GET" and action == "audio":
            if not row["audio_available"]:
                raise Problem(410, "The audio expired. The archive keeps the recording entry and transcript.")
            try:
                with open_audio(store.audio_path(identifier)) as stream:
                    audio = stream.read()
            except FileNotFoundError:
                raise Problem(410, "This clip’s audio is unavailable.") from None
            self.send(audio, mime="audio/mp4")
        elif method == "PUT" and action == "transcript" and device == row["device"]:
            value = self.object(60_000).get("text")
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 12000:
                raise ValueError("Enter a transcript up to 12,000 characters.")
            accepted = self.app.worker.local_transcript(identifier, device, value.strip())
            if not accepted:
                latest = store.recording(identifier)
                if latest and latest["state"] in {"queued", "processing", "awaiting_transcription"}:
                    raise Problem(409, "Excalibur is processing this recording. Retrying the upload is safe.")
            self.send({"accepted": accepted})
        elif method == "POST" and action == "retry" and self.server.admin:
            self.send({"queued": self.app.worker.retry(identifier)})
        else:
            raise Problem(404, "This Nex recording action was not found.")

    def voice_stream(self, identifier):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for event in self.app.worker.voice_streams.events(identifier):
            if event.get("reply") and not safe_voice_reply(event["reply"]):
                event = event | {"reply": None}
            if event["kind"] == "chime":
                event = event | {"audio": base64.b64encode(CHIME_WAV).decode("ascii")}
            elif event["kind"] == "speech":
                try:
                    sound = self.app.engines.synthesize(event["text"].strip(), event["language"])
                except ModelError:
                    sound = None
                event = event | {"audio": base64.b64encode(sound).decode("ascii") if sound else None}
            self.wfile.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()

    def handle_correction(self, method, identifier, device):
        store = self.app.store
        if method == "GET":
            result = store.transcript(identifier)
        elif method == "PUT":
            data = self.object(60_000)
            value, revision, request_id = data.get("text"), data.get("revision"), data.get("id")
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 12_000:
                raise ValueError("Enter a transcript up to 12,000 characters.")
            if type(revision) is not int or not 0 <= revision < 2**63:
                raise ValueError("Invalid transcript revision.")
            if not isinstance(request_id, str) or not re.fullmatch(UUID, request_id):
                raise ValueError("Invalid correction ID.")
            value = value.strip()
            current = store.transcript(identifier)
            note, engine = "", None
            if (current and current["state"] not in {"queued", "processing"}
                    and current["revision"] == revision and current["text"] != value):
                note, engine = self.app.assistant.process_notes(value, self.app.settings.read())
            result = store.correct_transcript(identifier, device, request_id, revision, value, note, engine=engine)
        else:
            raise Problem(405, "Use GET or PUT for transcript corrections.")
        if result is None:
            raise Problem(404, "Recording was not found.")
        self.send(result)

    def upload(self, identifier, device):
        duration = float(self.headers.get("X-Nex-Duration", "0"))
        mode = self.headers.get("X-Nex-Mode")
        start = timestamp(self.headers.get("X-Nex-Started", ""))
        language = valid_language(self.headers.get("X-Nex-Language", "auto"))
        zone = time_zone(self.headers.get("X-Nex-Time-Zone", "UTC")).key
        wake = self.headers.get("X-Nex-Wake") == "1"
        live_handled = self.headers.get("X-Nex-Live") == "1" and mode == "voice"
        conversation = self.headers.get("X-Nex-Watch-Conversation")
        if conversation is not None and (device != "watch" or mode != "voice" or live_handled
                                         or not re.fullmatch(UUID, conversation)):
            raise ValueError("Invalid Watch conversation.")
        if not math.isfinite(duration) or not 0 < duration <= 65 or mode not in {"ambient", "voice"}:
            raise ValueError("Invalid recording duration or mode.")
        if self.headers.get_content_type() != "audio/mp4":
            raise Problem(415, "Use AAC audio in an MPEG-4 container.")
        data = self.read(4_194_304)
        if len(data) < 32 or data[4:8] != b"ftyp":
            raise ValueError("The recording is missing its MPEG-4 header.")
        row, fresh = self.app.store.save_recording(
            identifier, device, start, duration, mode, wake, language, data, zone=zone, live_handled=live_handled,
            watch_conversation=conversation,
        )
        self.app.worker.wake.set()
        self.send(recorder_receipt(row), 201 if fresh else 200)

    def speech(self):
        if not self.app.speech_slot.acquire(blocking=False):
            raise Problem(429, "Piper is already speaking.")
        try:
            data = self.object(16_000)
            value = data.get("text")
            language = valid_language(data.get("language"))
            if not isinstance(value, str) or not 1 <= len(value) <= 1800 or language == "auto":
                raise ValueError("Invalid text or language for spoken reply.")
            try:
                audio = self.app.engines.synthesize(value, language)
            except ModelError as exc:
                raise Problem(503, str(exc)) from exc
            if audio is None:
                raise Problem(404, "No offline server voice is configured for this language.")
            self.send(audio, mime="audio/wav")
        finally:
            self.app.speech_slot.release()

    def record_gap(self, device):
        data = self.object(5000)
        identifier, detail = data.get("id"), data.get("text")
        if device == "console" or not isinstance(identifier, str) or not re.fullmatch(UUID, identifier):
            raise ValueError("Invalid gap ID.")
        if not isinstance(detail, str) or not 1 <= len(detail) <= 320:
            raise ValueError("Invalid recording event.")
        start = timestamp(data.get("started"))
        with self.app.store.connect() as db:
            existing = db.execute("SELECT device FROM memory WHERE id=?", ("g-" + identifier,)).fetchone()
            if existing and existing["device"] != device:
                raise RecordingConflict
            self.app.store.add_memory(db, "g-" + identifier, start, "gap", device, detail)
        self.send({"saved": True})

    def admin_models(self, method):
        if method == "PUT":
            self.app.settings.update(self.object(16_000))
            if self.server.hosted:
                self.server.hosted.configure()
            else:
                self.app.worker.configure()
        elif method != "GET":
            raise Problem(405, "Use GET or PUT to manage models.")
        self.send({"settings": self.app.settings.read(), "files": self.app.settings.files(),
                   "models": self.app.engines.model_status(), "chat_error": self.app.engines.last_chat_error})
