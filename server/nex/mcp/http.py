"""Bounded JSON and server-sent events for MCP HTTP transports."""

import http.client
import json
from contextlib import contextmanager
from time import monotonic

from . import MCPError

MAX_BODY = 512_000
ACCEPT = "application/json, text/event-stream"


def strict_json(body):
    def reject_constant(_):
        raise ValueError("Expected a JSON value.")

    try:
        return json.loads(body, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise MCPError("The MCP server returned invalid JSON.") from None


class HTTP:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.deadline = monotonic() + 15

    def remaining(self):
        if (seconds := self.deadline - monotonic()) <= 0:
            raise MCPError("The MCP server took too long to respond.")
        return seconds

    @contextmanager
    def request(self, method, parts, *, payload=None, headers=None, accepted=(200,)):
        options = {
            **self.endpoint.headers, "Accept": ACCEPT, "Accept-Encoding": "identity",
            "Connection": "close", **(headers or {}),
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()
            options["Content-Type"] = "application/json"
        target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        # Try alternate addresses only before any request bytes are sent.
        for address in self.endpoint.addresses:
            conn = self.endpoint.connect(address, min(self.remaining(), 4))
            try:
                conn.connect()
                break
            except OSError:
                conn.close()
        else:
            raise MCPError("Could not connect to the MCP server.")
        try:
            conn.sock.settimeout(self.remaining())
            conn.request(method, target, body=body, headers=options)
            response = conn.getresponse()
            with response:
                if response.status in (401, 403):
                    raise MCPError("The MCP server denied access. Check its authentication settings.")
                if response.status not in accepted:
                    raise MCPError(f"The MCP server returned HTTP {response.status}. Check its URL and transport.")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise MCPError("The MCP server returned an unsupported content encoding.")
                yield response
        except TimeoutError:
            raise MCPError("The MCP server took too long to respond.") from None
        except (OSError, http.client.HTTPException):
            raise MCPError("The MCP transport disconnected before Nex could confirm a result.") from None
        finally:
            conn.close()

    def chunks(self, response):
        size = 0
        while True:
            remaining = self.remaining()
            if sock := getattr(getattr(response.fp, "raw", None), "_sock", None):
                sock.settimeout(remaining)
            chunk = response.read1(8192)
            if not chunk:
                return
            size += len(chunk)
            if size > MAX_BODY:
                raise MCPError("The MCP response exceeded the size limit.")
            yield chunk

    def json(self, response):
        return strict_json(b"".join(self.chunks(response)))

    def events(self, response):
        if response.headers.get_content_type() != "text/event-stream":
            raise MCPError("The MCP server did not open an event stream.")
        pending, event, data = bytearray(), "message", []
        for chunk in self.chunks(response):
            pending.extend(chunk)
            while (index := pending.find(b"\n")) != -1:
                line = bytes(pending[:index]).rstrip(b"\r")
                del pending[:index + 1]
                if not line:
                    if data:
                        yield event, b"\n".join(data)
                    event, data = "message", []
                    continue
                field, _, value = line.partition(b":")
                value = value.removeprefix(b" ")
                if field == b"event":
                    event = value.decode("ascii", errors="replace")
                elif field == b"data":
                    data.append(value)
