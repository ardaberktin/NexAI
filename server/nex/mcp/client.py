"""Discover and invoke MCP tools over Streamable HTTP or legacy HTTP/SSE."""

from contextlib import ExitStack, contextmanager, suppress
from threading import BoundedSemaphore
from time import monotonic

from . import MCPError
from .endpoint import Endpoint
from .http import HTTP, strict_json

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
CLIENT = {"name": "Nex", "version": "1.0"}
SLOTS = BoundedSemaphore(6)
TRANSPORTS = {"http", "sse"}
MAX_TOOLS = 128


class Client:
    def __init__(self, endpoint, transport):
        self.endpoint = endpoint
        self.http = HTTP(endpoint)
        self.transport = transport
        self.post = endpoint.parts
        self.events = None
        self.version = ""
        self.session = ""
        self.sequence = 0

    def open(self, stack):
        if self.transport == "sse":
            response = stack.enter_context(self.http.request("GET", self.endpoint.parts,
                                                            headers={"Accept": "text/event-stream"}))
            self.events = self.http.events(response)
            for event, data in self.events:
                if event == "endpoint":
                    self.post = self.endpoint.target(data.decode("ascii", errors="replace"))
                    break
            else:
                raise MCPError("The MCP event stream did not advertise a message endpoint.")
        elif self.probe():
            return
        result = self.call("initialize", {"protocolVersion": LEGACY[0], "capabilities": {}, "clientInfo": CLIENT})
        self.version = result.get("protocolVersion")
        if self.version not in LEGACY:
            raise MCPError("The MCP server uses an unsupported protocol version.")
        self.send("notifications/initialized", {}, notify=True)

    def probe(self):
        self.version = MODERN
        envelope, status = self.send("server/discover", {}, probing=True)
        if status == 200 and isinstance(envelope.get("result"), dict):
            return True
        if isinstance(envelope.get("error"), dict) and envelope["error"].get("code") == -32022:
            data = envelope["error"].get("data")
            supported = data.get("supported") if isinstance(data, dict) else None
            if not isinstance(supported, list) or not any(version in supported for version in LEGACY):
                raise MCPError("The MCP server uses an unsupported protocol version.")
        self.version = ""
        return False

    def close(self):
        if self.session:
            self.http.deadline = min(self.http.deadline, monotonic() + 1)
            with suppress(MCPError), self.http.request("DELETE", self.post, headers=self.headers(), accepted=(200, 204, 405)):
                pass

    def headers(self):
        headers = {"MCP-Protocol-Version": self.version} if self.version else {}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        return headers

    def send(self, method, params, *, notify=False, probing=False, argument_headers=None):
        self.sequence += 1
        headers = self.headers()
        if self.version == MODERN:
            headers["Mcp-Method"] = method
            if method == "tools/call":
                headers["Mcp-Name"] = params["name"]
            headers.update(argument_headers or {})
            params = {**params, "_meta": {
                "io.modelcontextprotocol/protocolVersion": self.version,
                "io.modelcontextprotocol/clientInfo": CLIENT,
                "io.modelcontextprotocol/clientCapabilities": {},
            }}
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            payload["id"] = self.sequence
        accepted = (200, 202, 204, 400, 404, 405) if probing else (200, 202, 204)
        with self.http.request("POST", self.post, payload=payload, headers=headers, accepted=accepted) as response:
            status = response.status
            if method == "initialize" and self.transport == "http":
                session = response.headers.get("Mcp-Session-Id", "")
                if len(session) > 512 or any(not "!" <= char <= "~" for char in session):
                    raise MCPError("The MCP server returned an invalid session ID.")
                self.session = session
            if notify:
                return {}, status
            content_type = response.headers.get_content_type()
            if self.events is None and content_type == "text/event-stream":
                return self.match(self.http.events(response)), status
            if self.events is None and content_type == "application/json":
                data = self.http.json(response)
                if self.is_response(data):
                    return data, status
            if probing:
                return {}, status
        if self.events is not None:
            return self.match(self.events), status
        raise MCPError("The MCP server did not return a matching response.")

    def is_response(self, data):
        return (isinstance(data, dict) and data.get("jsonrpc") == "2.0"
                and type(data.get("id")) is int and data["id"] == self.sequence)

    def match(self, events):
        for event, raw in events:
            if event == "message" and raw and self.is_response(data := strict_json(raw)):
                return data
        raise MCPError("The MCP event stream ended before Nex received a result.")

    def call(self, method, params, **options):
        envelope, _ = self.send(method, params, **options)
        if "error" in envelope:
            raise MCPError("The MCP server rejected the request.")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise MCPError("The MCP server returned an invalid result.")
        if result.get("resultType") == "inputRequired":
            raise MCPError("The MCP tool requires additional interaction that Nex cannot provide.")
        return result

    def list_tools(self):
        cursor, seen, tools = None, set(), []
        for _ in range(20):
            result = self.call("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list) or any(not isinstance(tool, dict) for tool in page):
                raise MCPError("The MCP server returned an invalid tool list.")
            tools.extend(page)
            if len(tools) > MAX_TOOLS:
                raise MCPError("This MCP server provides more tools than Nex can load.")
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen:
                break
            seen.add(cursor)
        raise MCPError("The MCP server returned invalid or excessive tool list pages.")


@contextmanager
def connect(root, url, headers, transport):
    if not SLOTS.acquire(timeout=1):
        raise MCPError("Nex is already contacting several MCP servers. Try again shortly.")
    try:
        client = Client(Endpoint(root, url, headers), transport)
        with ExitStack() as stack:
            stack.callback(client.close)
            client.open(stack)
            yield client
    finally:
        SLOTS.release()
