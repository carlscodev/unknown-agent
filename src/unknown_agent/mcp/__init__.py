"""Model Context Protocol integration.

MCP is Unknown Agent's expansion system: external servers contribute tools,
resources and prompts that join the builtin registry under the same policy
engine, the same audit trail and the same risk vocabulary as everything else.

The package is layered so each piece is testable alone:

``models``     wire-neutral snapshots (ToolInfo, Capabilities, HealthReport…)
``client``     one server connection: transport, initialize, list, call, ping
``manager``    the fleet: config -> connections, trust, caps, fail-soft policy
``bridge``     MCP tools -> :class:`unknown_agent.tools.base.Tool` proxies
``catalog``    the curated ecosystem (filesystem, git, memory, fetch, …)

The official ``mcp`` SDK (2.x, 2026 spec line) is an *optional* dependency:
every import of it is guarded, and :func:`mcp_available` is the single truth
about whether the runtime can actually speak the protocol.
"""

from unknown_agent.mcp.models import (
    McpCapabilities,
    McpHealth,
    McpPromptInfo,
    McpResourceInfo,
    McpServerSnapshot,
    McpToolInfo,
)


def mcp_available() -> tuple[bool, str]:
    """Whether the official MCP SDK is importable, and if not, why."""

    try:
        import mcp  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on environment
        return False, f"the 'mcp' package is not installed ({exc}). Install with: pip install 'unknown-agent[mcp]'"
    return True, ""


__all__ = [
    "mcp_available",
    "McpCapabilities",
    "McpHealth",
    "McpPromptInfo",
    "McpResourceInfo",
    "McpServerSnapshot",
    "McpToolInfo",
]
