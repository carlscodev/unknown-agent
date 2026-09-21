"""The MCP fleet: configuration -> connections -> product surface.

The manager owns every configured server and is the *only* module that decides:

* which servers may run at all (enabled, not blocked, SDK present),
* which of their tools the agent may see (trust, allow/deny lists, caps),
* how failures behave (fail-soft by default: a dead server is a report line,
  never a dead run),
* and what everything looks like to `ua mcp …` (snapshots).

Concurrency: one manager per session/run.  Connections are created lazily and
serialised per server with an :class:`asyncio.Lock` because ``ClientSession`` is
not safe for interleaved requests from multiple tasks.
"""

from __future__ import annotations

import asyncio
from typing import Any

from unknown_agent.config.models import McpConfig, McpServerConfig, Settings
from unknown_agent.mcp.client import McpConnectError, McpConnection, mask_target
from unknown_agent.mcp.models import McpHealth, McpServerSnapshot, McpToolInfo
from unknown_agent.observability.logging import get_logger

log = get_logger("mcp.manager")

__all__ = ["McpManager", "select_tools"]


def select_tools(config: McpServerConfig, tools: list[McpToolInfo]) -> tuple[list[McpToolInfo], dict[str, str]]:
    """Apply allow/deny/cap filtering; return ``(kept, excluded{name: reason})``.

    Order matters and is documented behaviour: deny always beats allow, and the
    cap is applied last so the user's explicit choices are never the ones cut.
    """

    excluded: dict[str, str] = {}
    kept: list[McpToolInfo] = []
    allow = {name.strip() for name in config.allow_tools if name.strip()}
    deny = {name.strip() for name in config.deny_tools if name.strip()}
    for tool in tools:
        if tool.name in deny:
            excluded[tool.name] = "denied by [mcp.servers.*] deny_tools"
            continue
        if allow and tool.name not in allow:
            excluded[tool.name] = "not in allow_tools"
            continue
        kept.append(tool)
    if len(kept) > config.max_tools:
        for tool in kept[config.max_tools :]:
            excluded[tool.name] = f"over max_tools cap ({config.max_tools})"
        kept = kept[: config.max_tools]
    return kept, excluded


class McpManager:
    """Owns the configured MCP servers for one session."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config: McpConfig = settings.mcp
        self._connections: dict[str, McpConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._errors: dict[str, str] = {}

    # -- selection -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def server_names(self, *, include_disabled: bool = True) -> list[str]:
        names = sorted(self.config.servers)
        if include_disabled:
            return names
        return [name for name in names if self._runnable(name)]

    def server_config(self, name: str) -> McpServerConfig:
        try:
            return self.config.servers[name]
        except KeyError:
            known = ", ".join(sorted(self.config.servers)) or "none configured"
            raise KeyError(f"no MCP server named {name!r} (configured: {known})") from None

    def _runnable(self, name: str) -> bool:
        config = self.config.servers.get(name)
        return bool(self.enabled and config and config.enabled and config.trust != "blocked")

    # -- connections ---------------------------------------------------------
    def _lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    async def connection(self, name: str) -> McpConnection:
        """Get (connecting if needed) the live connection for ``name``."""

        config = self.server_config(name)
        if not self.enabled:
            raise McpConnectError("[mcp] is disabled")
        if not config.enabled:
            raise McpConnectError(f"server {name!r} is disabled - enable it with `ua mcp enable {name}`")
        if config.trust == "blocked":
            raise McpConnectError(f"server {name!r} is blocked - raise its trust with `ua mcp trust {name} <level>`")
        async with self._lock(name):
            conn = self._connections.get(name)
            if conn is None:
                conn = McpConnection(name, config)
                self._connections[name] = conn
            if not conn.connected:
                try:
                    await conn.connect()
                    self._errors.pop(name, None)
                except McpConnectError as exc:
                    self._errors[name] = str(exc)
                    raise
            return conn

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        conn = await self.connection(server)
        async with self._lock(server):
            return await conn.call_tool(tool, arguments)

    async def test(self, name: str) -> McpHealth:
        """Connection test used by `ua mcp test` and doctor.  Never raises."""

        try:
            conn = await self.connection(name)
        except (McpConnectError, KeyError) as exc:
            return McpHealth(ok=False, error=str(exc))
        async with self._lock(name):
            return await conn.ping()

    async def close(self, name: str | None = None) -> None:
        names = [name] if name else list(self._connections)
        for key in names:
            conn = self._connections.pop(key, None)
            if conn is not None:
                await conn.close()

    # -- product surface -----------------------------------------------------
    async def snapshot(self, name: str, *, connect: bool = True) -> McpServerSnapshot:
        """Everything `ua mcp show` needs.  Fail-soft: errors land in `.error`."""

        config = self.server_config(name)
        snap = McpServerSnapshot(
            name=name,
            transport=config.transport,
            target=mask_target(config),
            enabled=config.enabled,
            trust=config.trust,
            group=config.group,
            connected=False,
        )
        if not connect or not self._runnable(name):
            snap.error = self._errors.get(name, "" if self._runnable(name) else "not runnable (disabled, blocked, or [mcp] off)")
            return snap
        try:
            conn = await self.connection(name)
        except McpConnectError as exc:
            snap.error = str(exc)
            return snap
        snap.connected = True
        snap.capabilities = conn.capabilities
        async with self._lock(name):
            try:
                if conn.capabilities and conn.capabilities.tools:
                    all_tools = await conn.list_tools()
                    snap.tools, snap.excluded_tools = select_tools(config, all_tools)
                if conn.capabilities and conn.capabilities.resources:
                    snap.resources = await conn.list_resources()
                if conn.capabilities and conn.capabilities.prompts:
                    snap.prompts = await conn.list_prompts()
            except BaseException as exc:  # noqa: BLE001 - inspection must not crash
                snap.error = f"inspection failed: {exc}"
        return snap

    async def agent_tools(self) -> tuple[list[tuple[str, McpServerConfig, McpToolInfo]], list[str]]:
        """The tools the agent may use: ``[(server, config, tool)]`` + warnings.

        Applies per-server filtering, then the global ``max_total_tools`` cap.
        A server that fails to connect contributes a warning, not an exception
        (when ``fail_soft``; otherwise the McpConnectError propagates).
        """

        selected: list[tuple[str, McpServerConfig, McpToolInfo]] = []
        warnings: list[str] = []
        if not (self.enabled and self.config.expose_tools):
            return selected, warnings
        for name in self.server_names(include_disabled=False):
            config = self.server_config(name)
            try:
                conn = await self.connection(name)
            except McpConnectError as exc:
                if not self.config.fail_soft:
                    raise
                warnings.append(f"mcp server {name!r} unavailable: {exc}")
                continue
            if not (conn.capabilities and conn.capabilities.tools):
                continue
            async with self._lock(name):
                try:
                    tools = await conn.list_tools()
                except BaseException as exc:  # noqa: BLE001
                    if not self.config.fail_soft:
                        raise
                    warnings.append(f"mcp server {name!r}: listing tools failed: {exc}")
                    continue
            kept, _excluded = select_tools(config, tools)
            selected.extend((name, config, tool) for tool in kept)
        if len(selected) > self.config.max_total_tools:
            warnings.append(
                f"{len(selected)} MCP tools exceed [mcp] max_total_tools={self.config.max_total_tools}; extra tools dropped"
            )
            selected = selected[: self.config.max_total_tools]
        return selected, warnings

    def describe(self) -> dict[str, Any]:
        """Cheap, connection-free summary for `ua mcp list` and doctor."""

        servers = []
        for name in self.server_names():
            config = self.config.servers[name]
            conn = self._connections.get(name)
            servers.append(
                {
                    "name": name,
                    "transport": config.transport,
                    "target": mask_target(config),
                    "enabled": config.enabled,
                    "trust": config.trust,
                    "group": config.group,
                    "connected": bool(conn and conn.connected),
                    "error": self._errors.get(name, ""),
                    "description": config.description,
                }
            )
        return {"enabled": self.enabled, "servers": servers, "expose_tools": self.config.expose_tools}
