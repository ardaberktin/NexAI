"""Manage connections through the unlocked workspace API."""

import re

from . import connections

ROOT = "/v1/workspace/mcp"
ACTION = re.compile(ROOT + r"/(" + connections.ID.pattern + r")/(refresh|remove)$")
PERMISSION = re.compile(ROOT + r"/(" + connections.ID.pattern + r")/tool-access$")
ITEM = re.compile(ROOT + r"/(" + connections.ID.pattern + r")$")


def request(api, method, path, device, problem):
    if device not in {"mac", "iphone"}:
        raise problem(403, "Manage MCP connections from a signed-in Nex app.")
    store = api.app.store
    if path == ROOT:
        if method == "GET":
            return {"items": [item.display() for item in connections.list_connections(store)]}
        if method == "POST":
            return connections.add(store, api.object(64_000))
    if method == "PUT" and (match := PERMISSION.fullmatch(path)):
        return connections.update_permission(store, match[1], api.object(1024))
    if method == "PUT" and (match := ITEM.fullmatch(path)):
        return connections.update(store, match[1], api.object(256))
    if method == "POST" and (match := ACTION.fullmatch(path)):
        api.object(64)
        operation = connections.refresh if match[2] == "refresh" else connections.remove
        return operation(store, match[1])
    raise problem(404, "MCP settings route not found.")
