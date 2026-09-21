"""Wire-neutral MCP snapshots.

Everything the rest of the product needs to know about an MCP server is captured
in these dataclasses, so no other module has to import the SDK's pydantic types.
That keeps the SDK optional and keeps ``ua mcp … --json`` stable even if the SDK
changes its own model names again (it has: FastMCP -> MCPServer between 1.x and 2.x).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "McpToolInfo",
    "McpResourceInfo",
    "McpPromptInfo",
    "McpCapabilities",
    "McpHealth",
    "McpServerSnapshot",
]


@dataclass(slots=True)
class McpToolInfo:
    """One tool as advertised by a server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    #: Server-declared annotations (readOnlyHint, destructiveHint, …) from the
    #: 2025+ spec.  They are *hints* from an untrusted party: the bridge maps
    #: them onto a risk level but trust/policy always has the final word.
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only_hint(self) -> bool:
        return bool(self.annotations.get("readOnlyHint", False))

    @property
    def destructive_hint(self) -> bool:
        # The spec default for destructiveHint is true when absent - a server
        # that says nothing must be treated as if it could destroy things.
        value = self.annotations.get("destructiveHint")
        return True if value is None else bool(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "annotations": self.annotations,
        }


@dataclass(slots=True)
class McpResourceInfo:
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"uri": self.uri, "name": self.name, "description": self.description, "mime_type": self.mime_type}


@dataclass(slots=True)
class McpPromptInfo:
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "arguments": self.arguments}


@dataclass(slots=True)
class McpCapabilities:
    """What one server can do, learned at initialize time."""

    tools: bool = False
    resources: bool = False
    prompts: bool = False
    logging: bool = False
    completions: bool = False
    #: listChanged notifications supported per surface.
    tools_list_changed: bool = False
    resources_subscribe: bool = False
    server_name: str = ""
    server_version: str = ""
    protocol_version: str = ""
    instructions: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": self.tools,
            "resources": self.resources,
            "prompts": self.prompts,
            "logging": self.logging,
            "completions": self.completions,
            "tools_list_changed": self.tools_list_changed,
            "resources_subscribe": self.resources_subscribe,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "instructions": self.instructions,
        }


@dataclass(slots=True)
class McpHealth:
    """Result of a connection test / ping."""

    ok: bool
    latency_ms: float = 0.0
    error: str = ""
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "latency_ms": round(self.latency_ms, 1), "error": self.error, "checked_at": self.checked_at}


@dataclass(slots=True)
class McpServerSnapshot:
    """Everything ``ua mcp show <name>`` renders, in one serialisable object."""

    name: str
    transport: str
    target: str  # command line or URL, secrets already masked
    enabled: bool
    trust: str
    group: str
    connected: bool
    capabilities: McpCapabilities | None = None
    tools: list[McpToolInfo] = field(default_factory=list)
    resources: list[McpResourceInfo] = field(default_factory=list)
    prompts: list[McpPromptInfo] = field(default_factory=list)
    health: McpHealth | None = None
    #: Tools filtered out and why: {"name": "reason"} - the audit answer to
    #: "why can't the agent see tool X?"
    excluded_tools: dict[str, str] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "target": self.target,
            "enabled": self.enabled,
            "trust": self.trust,
            "group": self.group,
            "connected": self.connected,
            "capabilities": self.capabilities.to_dict() if self.capabilities else None,
            "tools": [tool.to_dict() for tool in self.tools],
            "resources": [item.to_dict() for item in self.resources],
            "prompts": [item.to_dict() for item in self.prompts],
            "health": self.health.to_dict() if self.health else None,
            "excluded_tools": dict(self.excluded_tools),
            "error": self.error,
        }
