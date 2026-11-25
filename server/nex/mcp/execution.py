"""Recheck MCP tool metadata and workspace permissions at dispatch."""

from ..accounts import write_tools
from . import MCPError, client
from .client import MODERN
from .connections import discovered_tool, listed, redact
from .tool import validate_arguments, validate_call


def current_tool(db, alias, *, scheduled=False):
    for connection in listed(db):
        for tool in connection.tools:
            if alias == tool.alias(connection.id) and connection.permits(tool, scheduled=scheduled):
                return connection, tool
    raise MCPError("This MCP tool is disabled, has changed, or is unavailable with its current permissions.")


def call_profile(store, alias, *, scheduled=False):
    if entry := write_tools.ACTIONS.get(alias):
        return write_tools.profile(store.root, entry, scheduled=scheduled)
    with store.connect() as db:
        connection, tool = current_tool(db, alias, scheduled=scheduled)
        return connection.name, tool, connection.permission(tool).access


def review_arguments(call):
    arguments = call["arguments"]
    if entry := write_tools.ACTIONS.get(call["name"]):
        return write_tools.validated_arguments(entry.tool, arguments)
    return arguments


def account_write_already_attempted(alias, attempts):
    entry = write_tools.ACTIONS.get(alias)
    return entry is not None and any(
        (previous := write_tools.ACTIONS.get(name)) is not None and previous.account == entry.account
        for name in attempts
    )


def live_tool(client, advertised, secrets):
    current = next((item for item in client.list_tools() if item.get("name") == advertised.name), None)
    if current is None:
        raise MCPError("The MCP server no longer advertises this tool.")
    tool = discovered_tool(current, secrets)
    if tool.read_only != advertised.read_only:
        raise MCPError("The MCP server changed this tool’s read/write capabilities. Refresh its tools in Settings.")
    if not tool.read_only and tool != advertised:
        raise MCPError("The MCP server changed this write tool. Refresh its tools in Settings.")
    return tool


def execute(store, call, *, scheduled=False, approval=None, write_run=None):
    validate_call(call)
    alias, arguments = call["name"], call["arguments"]
    if entry := write_tools.ACTIONS.get(alias):
        return write_tools.execute(store.root, entry, call, scheduled=scheduled, approval=approval, write_run=write_run)
    with store.connect() as db:
        connection, advertised = current_tool(db, alias, scheduled=scheduled)
    if not advertised.read_only:
        if approval is None or not approval.matches(advertised, connection.permission(advertised)):
            raise MCPError("This MCP tool requires authorization for the requested operation.")
        if scheduled and write_run is None:
            raise MCPError("A saved run is required for this MCP tool.")
        if write_run is not None and (saved := write_run.receipt(alias)) is not None:
            return saved
    with client.connect(store.root, connection.url, connection.headers, connection.transport) as session:
        tool = live_tool(session, advertised, connection.redactions())
        validate_arguments(tool, arguments)
        headers = tool.argument_headers(arguments) if session.version == MODERN else {}

        def authorized(db):
            saved_connection, saved_tool = current_tool(db, alias, scheduled=scheduled)
            if saved_tool != advertised:
                raise MCPError("The MCP tool changed before it could run.")
            if approval is not None and not approval.matches(saved_tool, saved_connection.permission(saved_tool)):
                raise MCPError("The MCP tool’s permissions changed before it could run.")

        def invoke():
            result = session.call("tools/call", {"name": tool.name, "arguments": arguments}, argument_headers=headers)
            data = outcome(connection, tool, result)
            return data | {"operation": approval.operation} if approval is not None else data

        if not tool.read_only and write_run is not None:
            return write_run.dispatch(call, connection.name, tool.name, approval.operation, authorized, invoke)
        with store.connect() as db:
            authorized(db)
        return invoke()


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
