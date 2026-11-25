"""Persist private, account-scoped MCP settings and the last discovered tool catalog."""

import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import unquote, urlsplit

from . import MCPError
from .client import MODERN, TRANSPORTS, connect
from .endpoint import endpoint_parts, headers_for
from .tool import Tool, validate_arguments, validate_call

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_connections (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, headers TEXT NOT NULL,
    transport TEXT NOT NULL, tools TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    allow_goals INTEGER NOT NULL DEFAULT 0
);
"""
MAX_CONNECTIONS = 20
MAX_WORKSPACE_TOOLS = 128
ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")


@dataclass(frozen=True)
class Connection:
    id: str
    name: str
    url: str
    headers: dict
    transport: str
    tools: tuple[Tool, ...]
    enabled: bool
    allow_goals: bool

    def display(self):
        return {
            "id": self.id, "name": self.name, "host": urlsplit(self.url).hostname, "transport": self.transport,
            "enabled": self.enabled, "allow_goals": self.allow_goals, "has_credentials": bool(self.headers),
            "tools": [{"name": tool.name, "read_only": tool.read_only} for tool in self.tools],
        }

    def redactions(self):
        parts = urlsplit(self.url)
        candidates = [*self.headers.values(), parts.query, unquote(parts.query)]
        candidates += [value.partition(" ")[2] for name, value in self.headers.items() if name == "authorization"]
        candidates += [unquote(value) for entry in parts.query.split("&") if (value := entry.partition("=")[2])]
        return sorted({secret for secret in candidates if secret}, key=len, reverse=True)


def from_row(row):
    return Connection(row["id"], row["name"], row["url"], json.loads(row["headers"]), row["transport"],
                      tuple(Tool(**tool) for tool in json.loads(row["tools"])),
                      bool(row["enabled"]), bool(row["allow_goals"]))


def list_connections(store):
    with store.connect() as db:
        return [from_row(row) for row in db.execute("SELECT * FROM mcp_connections ORDER BY name COLLATE NOCASE,id")]


def catalog(store, *, scheduled=False):
    return {tool.alias(connection.id): (connection.name, tool)
            for connection in list_connections(store) if connection.enabled and (not scheduled or connection.allow_goals)
            for tool in connection.tools if not scheduled or tool.read_only}


def lookup(db, identifier):
    row = db.execute("SELECT * FROM mcp_connections WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise MCPError("The MCP connection is no longer available.")
    return from_row(row)


def check_capacity(db, identifier, tools):
    count, tool_count = db.execute(
        "SELECT count(*),coalesce(sum(json_array_length(tools)),0) FROM mcp_connections WHERE id!=?", (identifier,),
    ).fetchone()
    if count >= MAX_CONNECTIONS or tool_count + len(tools) > MAX_WORKSPACE_TOOLS:
        raise MCPError("This workspace can store up to 20 MCP connections and 128 tools in total.")


def add(store, data):
    if data.keys() - {"id", "name", "url", "headers", "transport"}:
        raise MCPError("Use a name, server URL, transport, and optional HTTP headers.")
    identifier, name, url = data.get("id"), data.get("name"), data.get("url")
    if not isinstance(identifier, str) or not ID.fullmatch(identifier):
        raise MCPError("Invalid MCP connection ID.")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or not name.strip().isprintable():
        raise MCPError("Enter a server name of up to 60 characters.")
    transport = data.get("transport", "http")
    if not isinstance(transport, str) or transport not in TRANSPORTS:
        raise MCPError("Choose Streamable HTTP or HTTP with SSE.")
    endpoint_parts(url)
    headers = headers_for(data.get("headers", {}))
    new = Connection(identifier, name.strip(), url, headers, transport, (), True, False)
    with store.connect() as db:
        existing = db.execute("SELECT * FROM mcp_connections WHERE id=?", (identifier,)).fetchone()
        if existing:
            return check_same(from_row(existing), new)
        check_capacity(db, identifier, ())
    tools = discover(store, new)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM mcp_connections WHERE id=?", (identifier,)).fetchone()
        if existing:
            return check_same(from_row(existing), new)
        check_capacity(db, identifier, tools)
        db.execute("INSERT INTO mcp_connections(id,name,url,headers,transport,tools) VALUES(?,?,?,?,?,?)",
                   (identifier, new.name, url, json.dumps(headers), transport, encode_tools(tools)))
        return lookup(db, identifier).display()


def check_same(existing, new):
    if (existing.name, existing.url, existing.headers, existing.transport) != (new.name, new.url, new.headers, new.transport):
        raise MCPError("This MCP connection ID already belongs to another configuration.")
    return existing.display()


def encode_tools(tools):
    return json.dumps([asdict(tool) for tool in tools], ensure_ascii=True)


def update(store, identifier, data):
    if data.keys() - {"enabled", "allow_goals"} or not data or any(type(value) is not bool for value in data.values()):
        raise MCPError("Choose whether the MCP connection and its use in goals are enabled.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        connection = lookup(db, identifier)
        db.execute("UPDATE mcp_connections SET enabled=?,allow_goals=? WHERE id=?",
                   (data.get("enabled", connection.enabled), data.get("allow_goals", connection.allow_goals), identifier))
        return lookup(db, identifier).display()


def remove(store, identifier):
    with store.connect() as db:
        db.execute("DELETE FROM mcp_connections WHERE id=?", (identifier,))
    return {"removed": True}


def refresh(store, identifier):
    with store.connect() as db:
        connection = lookup(db, identifier)
    tools = discover(store, connection)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = lookup(db, identifier)
        if current.tools != connection.tools:
            return current.display()
        check_capacity(db, identifier, tools)
        db.execute("UPDATE mcp_connections SET tools=? WHERE id=?", (encode_tools(tools), identifier))
        return lookup(db, identifier).display()


def redact(value, secrets, depth=0):
    if depth > 48:
        raise MCPError("The MCP server returned data with too many nesting levels.")
    if isinstance(value, str):
        for secret in secrets:
            if value == secret or len(secret) >= 4:
                value = value.replace(secret, "[redacted]")
        return value.encode("utf-8", errors="replace").decode()
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets, depth + 1) for item in value]
    return value


def discover(store, connection):
    with connect(store.root, connection.url, connection.headers, connection.transport) as client:
        raw = client.list_tools()
    secrets = connection.redactions()
    tools = [Tool.discovered(value | {"description": redact(value.get("description"), secrets),
                                      "inputSchema": redact(value.get("inputSchema"), secrets)}) for value in raw]
    if len({tool.name for tool in tools}) != len(tools):
        raise MCPError("The MCP server listed a tool name more than once.")
    if any(redact(tool.name, secrets) != tool.name for tool in tools):
        raise MCPError("The MCP server exposed a credential in its tool catalog.")
    return tools


def current_tool(store, alias, *, scheduled):
    for connection in list_connections(store):
        if not connection.enabled or (scheduled and not connection.allow_goals):
            continue
        for tool in connection.tools:
            if alias == tool.alias(connection.id) and (not scheduled or tool.read_only):
                return connection, tool
    raise MCPError("This MCP tool is disabled, has changed, or is no longer available.")


def execute(store, call, *, scheduled=False):
    validate_call(call)
    alias, arguments = call["name"], call["arguments"]
    connection, advertised = current_tool(store, alias, scheduled=scheduled)
    tool = advertised
    with connect(store.root, connection.url, connection.headers, connection.transport) as client:
        if scheduled:
            current = next((item for item in client.list_tools() if item.get("name") == tool.name), None)
            if not current or not (tool := Tool.discovered(current)).read_only:
                raise MCPError("The MCP server no longer advertises this tool as read-only.")
        _, saved_tool = current_tool(store, alias, scheduled=scheduled)
        if saved_tool != advertised:
            raise MCPError("The MCP tool changed before it could run.")
        validate_arguments(tool, arguments)
        headers = tool.argument_headers(arguments) if client.version == MODERN else {}
        result = client.call("tools/call", {"name": tool.name, "arguments": arguments}, argument_headers=headers)
    return outcome(connection, tool, result)


def outcome(connection, tool, result):
    text = []
    blocks = result.get("content")
    for block in blocks if isinstance(blocks, list) else ():
        if not isinstance(block, dict):
            continue
        item = block.get("resource") if block.get("type") == "resource" else block
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text.append(item["text"])
    data = {"server": connection.name, "tool": tool.name,
            "status": "error" if result.get("isError") is True else "ok",
            "text": "\n".join(text)[:12_000]}
    if result.get("structuredContent") is not None:
        data["structured_content"] = result["structuredContent"]
    if data["status"] == "ok" and not text and "structured_content" not in data and blocks:
        data["status"] = "unsupported_content"
    return redact(data, connection.redactions())
