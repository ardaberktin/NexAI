"""Advertise and authorize write tools for the account's built-in integrations."""

from collections.abc import Callable
from dataclasses import dataclass

from ..mcp import MCPError
from ..mcp.permissions import Access, Permission
from ..mcp.tool import Tool
from .credentials import (
    LABELS,
    PROVIDERS,
    TICK_TASKS,
    WRITE_SCOPES,
    Credentials,
    write_settings,
)
from .http import AccountError
from .write_actions import complete_task, create_event, create_task, send_email


@dataclass(frozen=True)
class AccountWrite:
    account: str
    tool: Tool
    invoke: Callable

    @property
    def server(self):
        return LABELS[self.account]

    @property
    def provider(self):
        return PROVIDERS[self.account]


def action(account, handler, description, fields, *, optional=()):
    properties = {name: {"type": "string", "description": purpose, "minLength": 1, "maxLength": limit}
                  for name, (purpose, limit) in fields.items()}
    if "due" in properties:
        properties["due"]["format"] = "date-time"
    if "list" in properties:
        properties["list"]["default"] = "Inbox"
    for name in optional:
        field = properties[name]
        properties[name] = {"anyOf": [field, {"type": "null"}],
                            "description": field["description"] + ". Omit this field or use JSON null if unspecified."}
    schema = {"type": "object", "properties": properties, "required": [name for name in fields if name not in optional],
              "additionalProperties": False}
    return AccountWrite(account, Tool(handler.__name__, description, schema, False), handler)


TASK_FIELDS = {"title": ("Exact task title", 300), "list": ("Exact TickTick list name, default Inbox", 200)}
ACTIONS = {entry.tool.alias("connected-accounts/" + entry.account): entry for entry in (
    action("gmail", send_email, "Send a new plain-text Gmail message to one explicit email address.", {
        "to": ("One recipient email address", 254), "subject": ("Message subject", 300), "body": ("Message text", 6000),
    }),
    action("calendar", create_event, "Create an event on the primary Google Calendar with explicit start and end.", {
        "title": ("Event title", 300), "start": ("ISO date or timestamp with its UTC offset", 40),
        "end": ("ISO date (exclusive for all-day events) or timestamp with its UTC offset", 40),
        "description": ("Event description", 3000), "location": ("Event location", 300),
    }, optional=("description", "location")),
    action("ticktick", create_task, "Add a TickTick task. The Inbox is the default list.", TASK_FIELDS | {
        "content": ("Task notes", 3000), "due": ("Due timestamp with its UTC offset", 40),
    }, optional=("list", "content", "due")),
    action("ticktick", complete_task, "Mark an existing TickTick task complete by exact title. The Inbox is the default list.",
           TASK_FIELDS, optional=("list",)),
)}


def available(store, *, scheduled):
    settings = Credentials(store.root).write_access()
    return {alias: (entry.server, entry.tool) for alias, entry in ACTIONS.items()
            if entry.account in settings and (not scheduled or settings[entry.account])}


def require_write(saved, entry, scheduled):
    settings = write_settings(saved, entry.provider)
    if entry.account not in settings or (scheduled and not settings[entry.account]):
        raise MCPError("Write access for this connected account is disabled in Settings.")
    return Permission(Access.WRITE, settings[entry.account])


def profile(root, entry, *, scheduled):
    permission = require_write(Credentials(root).read(entry.provider), entry, scheduled)
    return entry.server, entry.tool, permission.access


def validated_arguments(tool, args):
    fields = tool.schema["properties"]
    required = set(tool.schema["required"])
    if not required <= args.keys() <= fields.keys():
        raise MCPError("The connected-account tool is missing required fields or contains unexpected fields.")
    cleaned = {}
    for key, value in args.items():
        if value is None and key not in required:
            continue
        field = fields[key] if key in required else fields[key]["anyOf"][0]
        if not isinstance(value, str) or len(value) > field["maxLength"] or "\0" in value:
            raise MCPError(f"The connected-account tool supplied an invalid {key}.")
        if not value.strip():
            if key in required:
                raise MCPError(f"The connected-account tool supplied an empty {key}.")
            continue
        try:
            value.encode()
        except UnicodeError:
            raise MCPError(f"The connected-account tool supplied invalid text in {key}.") from None
        cleaned[key] = value if key in {"body", "content", "description"} else value.strip()
    if "list" in fields:
        cleaned.setdefault("list", "Inbox")
    return cleaned


def execute(root, entry, call, *, scheduled, approval, write_run):
    vault = Credentials(root)
    args = validated_arguments(entry.tool, call["arguments"])

    def authorize(_db=None):
        saved = vault.read(entry.provider)
        permission = require_write(saved, entry, scheduled)
        if approval is None or not approval.matches(entry.tool, permission):
            raise MCPError("This connected-account tool requires authorization for the requested operation.")
        return saved

    def invoke():
        with vault.locked(entry.provider):
            saved = authorize()
            try:
                scopes = {WRITE_SCOPES[entry.account]}
                if entry.account == "ticktick":
                    scopes.add(TICK_TASKS)
                token = vault.token_locked(entry.provider, saved, scopes)
                result = entry.invoke(token, args)
            except AccountError as error:
                return {"server": entry.server, "tool": entry.tool.name, "operation": Access.WRITE,
                        "status": "error", "detail": str(error)}
        return {"server": entry.server, "tool": entry.tool.name, "operation": Access.WRITE,
                "status": "ok", "structured_content": result}

    if scheduled and write_run is None:
        raise MCPError("A saved goal run is required for a connected-account write.")
    if write_run is not None:
        return write_run.dispatch(call, entry.server, entry.tool.name, Access.WRITE, authorize, invoke)
    return invoke()
