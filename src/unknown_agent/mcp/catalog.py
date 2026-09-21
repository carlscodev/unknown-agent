"""The curated MCP ecosystem: one entry per recommended server.

Like the provider catalog, every entry is *usable*, not decorative: it declares
exactly how to launch or reach the server, what it needs (runtime, credentials,
paths), and a sensible default trust level.  ``ua mcp add <key>`` and the
onboarding MCP step are generated from this file, so catalog and CLI can never
drift apart.

Reference servers follow the official modelcontextprotocol/servers conventions:
Python servers launch with ``uvx``, TypeScript servers with ``npx -y``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unknown_agent.config.models import McpServerConfig, McpTrustLevel

__all__ = ["McpCatalogEntry", "MCP_CATALOG", "catalog_entry", "catalog_payload", "config_from_entry"]


@dataclass(frozen=True, slots=True)
class McpCatalogEntry:
    key: str
    label: str
    description: str
    command: str
    args: tuple[str, ...]
    #: ``uv`` or ``node`` - what has to exist on PATH.
    runtime: str
    default_trust: McpTrustLevel = "trusted"
    #: Args templated with ``{project_root}`` get substituted at add time.
    needs_project_root: bool = False
    env_hints: dict[str, str] = field(default_factory=dict)
    group: str = "core"
    docs_url: str = ""
    recommended: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "command": self.command,
            "args": list(self.args),
            "runtime": self.runtime,
            "default_trust": self.default_trust,
            "group": self.group,
            "docs_url": self.docs_url,
            "recommended": self.recommended,
            "env_hints": dict(self.env_hints),
        }


MCP_CATALOG: tuple[McpCatalogEntry, ...] = (
    McpCatalogEntry(
        key="filesystem",
        label="Filesystem",
        description="Read/write access to configured directories (reference server).",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", "{project_root}"),
        runtime="node",
        default_trust="trusted",
        needs_project_root=True,
        group="core",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        recommended=False,  # the builtin fs tools already cover the workspace
    ),
    McpCatalogEntry(
        key="git",
        label="Git",
        description="Repository inspection and manipulation: log, diff, branch, blame.",
        command="uvx",
        args=("mcp-server-git", "--repository", "{project_root}"),
        runtime="uv",
        default_trust="trusted",
        needs_project_root=True,
        group="core",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/git",
        recommended=True,
    ),
    McpCatalogEntry(
        key="memory",
        label="Memory (knowledge graph)",
        description="Persistent knowledge-graph memory across sessions.",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
        runtime="node",
        default_trust="trusted",
        group="knowledge",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        recommended=False,  # the product ships its own memory; offer, don't push
    ),
    McpCatalogEntry(
        key="fetch",
        label="Fetch (web)",
        description="Fetch a URL and convert its content to markdown for the model.",
        command="uvx",
        args=("mcp-server-fetch",),
        runtime="uv",
        default_trust="trusted",
        group="web",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        recommended=True,
    ),
    McpCatalogEntry(
        key="sequential-thinking",
        label="Sequential thinking",
        description="Structured step-by-step reasoning scratchpad for hard problems.",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-sequential-thinking"),
        runtime="node",
        default_trust="full",  # pure reasoning, no side effects
        group="reasoning",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
        recommended=True,
    ),
    McpCatalogEntry(
        key="time",
        label="Time",
        description="Timezone-aware current time and conversions.",
        command="uvx",
        args=("mcp-server-time",),
        runtime="uv",
        default_trust="full",
        group="utility",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/time",
    ),
    McpCatalogEntry(
        key="everything",
        label="Everything (test server)",
        description="Exercises every MCP feature - useful for testing the integration, not for work.",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-everything"),
        runtime="node",
        default_trust="sandboxed",
        group="dev",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/everything",
    ),
)

_INDEX = {entry.key: entry for entry in MCP_CATALOG}


def catalog_entry(key: str) -> McpCatalogEntry | None:
    return _INDEX.get((key or "").strip().lower())


def catalog_payload() -> list[dict[str, object]]:
    return [entry.to_dict() for entry in MCP_CATALOG]


def config_from_entry(entry: McpCatalogEntry, *, project_root: str | None = None) -> McpServerConfig:
    """Build a ready-to-write server config from a catalog entry."""

    args = [arg.format(project_root=project_root or ".") if "{project_root}" in arg else arg for arg in entry.args]
    return McpServerConfig(
        transport="stdio",
        command=entry.command,
        args=args,
        trust=entry.default_trust,
        group=entry.group,
        description=entry.description,
        origin=f"catalog:{entry.key}",
    )
