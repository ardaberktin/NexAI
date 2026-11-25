"""Workspace grants for discovered MCP tools."""

from dataclasses import dataclass
from enum import StrEnum

from . import MCPError
from .tool import Tool

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_tool_permissions (
    connection_id TEXT NOT NULL, tool_name TEXT NOT NULL,
    access TEXT NOT NULL, allow_goal_writes INTEGER NOT NULL,
    PRIMARY KEY(connection_id,tool_name)
);
"""


class Access(StrEnum):
    OFF = "off"
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"

    @property
    def allows_write(self):
        return self in (Access.WRITE, Access.READ_WRITE)

    @property
    def allows_read(self):
        return self in (Access.READ, Access.READ_WRITE)


@dataclass(frozen=True)
class Permission:
    access: Access
    allow_goal_writes: bool = False

    @classmethod
    def default(cls, tool):
        return cls(Access.READ if tool.read_only else Access.OFF)

    def changed(self, data, tool):
        try:
            access = Access(data.get("access", self.access))
        except (ValueError, TypeError):
            raise MCPError("Choose Off, Read only, Write only, or Read & write.") from None
        goals = data.get("allow_goal_writes", self.allow_goal_writes and not tool.read_only and access.allows_write)
        if type(goals) is not bool or (goals and (tool.read_only or not access.allows_write)):
            raise MCPError("Enable write access for a tool that can make changes before allowing its use in goals.")
        return Permission(access, goals)

    def permits(self, tool, *, scheduled, allow_goal_reads):
        if tool.read_only:
            return self.access.allows_read and (not scheduled or allow_goal_reads)
        return self.access.allows_write and (not scheduled or self.allow_goal_writes)


@dataclass(frozen=True)
class CallApproval:
    tool: Tool
    operation: Access

    def matches(self, tool, permission):
        return self.tool == tool and (
            self.operation == Access.WRITE or self.operation == Access.READ and permission.access.allows_read
        )


def configured(db, connection_id):
    return {
        row["tool_name"]: Permission(Access(row["access"]), bool(row["allow_goal_writes"]))
        for row in db.execute("SELECT * FROM mcp_tool_permissions WHERE connection_id=?", (connection_id,))
    }


def save(db, connection_id, tool_name, permission):
    db.execute(
        "INSERT INTO mcp_tool_permissions VALUES(?,?,?,?) "
        "ON CONFLICT(connection_id,tool_name) DO UPDATE SET access=excluded.access,allow_goal_writes=excluded.allow_goal_writes",
        (connection_id, tool_name, permission.access, permission.allow_goal_writes),
    )
