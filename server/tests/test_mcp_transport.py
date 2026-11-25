"""Wire fixtures for MCP HTTP compatibility and endpoint restrictions."""

import json
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from unittest.mock import patch

from nex.mcp import MCPError
from nex.mcp.client import MODERN, connect
from nex.mcp.endpoint import Endpoint, headers_for
from nex.mcp.tool import Tool

TOOL = {"name": "catalog", "inputSchema": {"type": "object", "properties": {
    "region": {"type": "string", "x-mcp-header": "Region"},
}}}


class MCPFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        location = b"https://other.example/send" if self.server.mode == "foreign" else b"/send?session=fixture"
        self.event(b"endpoint", location)
        while True:
            try:
                payload = self.server.events.get(timeout=1)
                if payload is None:
                    break
                self.event(b"message", payload)
            except (Empty, OSError):
                break

    def do_DELETE(self):
        self.server.calls.append(("delete", self.headers.get("Mcp-Session-Id")))
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        obj = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, params = obj["method"], obj["params"]
        self.server.calls.append((method, params, dict(self.headers)))
        status = 200
        message = {"jsonrpc": "2.0", "id": obj.get("id")}
        if method == "server/discover":
            if self.server.mode == "modern":
                message["result"] = {"capabilities": {"tools": {}}}
            else:
                status = 400
                message["error"] = {"code": -32601}
        elif method == "initialize":
            message["result"] = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            message["result"] = {"tools": [TOOL]} if params.get("cursor") else {"tools": [], "nextCursor": "next"}
        elif method == "tools/call":
            message["result"] = {"content": [{"type": "text", "text": "Inventory"}]}
        if self.server.mode == "sse" or "id" not in obj:
            self.send_response(202)
            self.end_headers()
            if "id" in obj:
                self.server.events.put(json.dumps(message).encode())
            return
        by_event = self.server.mode == "session" and method == "tools/call"
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if by_event else "application/json")
        if method == "initialize":
            self.send_header("Mcp-Session-Id", "fixture-session")
        self.end_headers()
        if by_event:
            self.event(b"message", b'{"jsonrpc":"2.0","method":"notifications/progress"}')
            self.event(b"message", json.dumps(message).encode())
        else:
            self.wfile.write(json.dumps(message).encode())

    def event(self, event, data):
        self.wfile.write(b"event: " + event + b"\ndata: " + data + b"\n\n")
        self.wfile.flush()


class MCPTransportTests(unittest.TestCase):
    def fixture(self, mode):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        server = ThreadingHTTPServer(("127.0.0.1", 0), MCPFixture)
        server.mode, server.calls, server.events = mode, [], Queue()
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.events.put, None)
        url = f"http://127.0.0.1:{server.server_port}/mcp"
        (root / "mcp-loopback.json").write_text(json.dumps([url]))
        return root, url, server

    def test_modern_paginated_discovery_and_call_headers(self):
        root, url, server = self.fixture("modern")
        with connect(root, url, {"authorization": "Bearer fixture"}, "http") as client:
            self.assertEqual(client.version, MODERN)
            self.assertEqual(client.list_tools(), [TOOL])
            args = {"region": "Île-de-France"}
            headers = Tool.discovered(TOOL).argument_headers(args)
            result = client.call("tools/call", {"name": "catalog", "arguments": args}, argument_headers=headers)
        self.assertEqual(result["content"][0]["text"], "Inventory")
        self.assertEqual([call[0] for call in server.calls], ["server/discover", "tools/list", "tools/list", "tools/call"])
        _, params, sent = server.calls[-1]
        self.assertEqual(params["_meta"]["io.modelcontextprotocol/protocolVersion"], MODERN)
        self.assertEqual(sent["Mcp-Name"], "catalog")
        self.assertEqual(sent["Mcp-Param-region"], "=?base64?w45sZS1kZS1GcmFuY2U=?=")
        self.assertEqual(sent["authorization"], "Bearer fixture")

    def test_legacy_handshake_session_and_streamed_tool_result(self):
        root, url, server = self.fixture("session")
        with connect(root, url, {}, "http") as client:
            self.assertEqual(client.version, "2025-11-25")
            self.assertEqual(client.list_tools(), [TOOL])
            client.call("tools/call", {"name": "catalog", "arguments": {}})
        self.assertEqual(server.calls[-1], ("delete", "fixture-session"))
        self.assertEqual(server.calls[-2][2]["Mcp-Session-Id"], "fixture-session")

    def test_legacy_sse_posts_to_the_server_advertised_same_origin(self):
        root, url, server = self.fixture("sse")
        with connect(root, url, {}, "sse") as client:
            self.assertEqual(client.list_tools(), [TOOL])
        self.assertEqual([call[0] for call in server.calls],
                         ["initialize", "notifications/initialized", "tools/list", "tools/list"])

    def test_foreign_sse_endpoint_is_rejected_before_posting(self):
        root, url, server = self.fixture("foreign")
        with self.assertRaisesRegex(MCPError, "different origin"), connect(root, url, {}, "sse"):
            pass
        self.assertEqual(server.calls, [])

    def test_private_addresses_and_transport_header_overrides_are_blocked(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with self.assertRaises(MCPError):
            Endpoint(root, "http://127.0.0.1:1234/mcp", {})
        private_record = (2, 1, 6, "", ("10.0.0.1", 443))
        with patch("nex.web_transport.socket.getaddrinfo", return_value=[private_record]), self.assertRaises(MCPError):
            Endpoint(root, "https://rebound.example/mcp", {})
        for headers in ({"Host": "alternate.example"}, {"x-mcp": "line\nbreak"}):
            with self.assertRaises(MCPError):
                headers_for(headers)
