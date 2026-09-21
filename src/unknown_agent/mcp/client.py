"""One MCP server connection.

Wraps the official SDK's transports (stdio, streamable HTTP, SSE) and
:class:`mcp.ClientSession` behind a small, product-shaped surface:

* :meth:`McpConnection.connect` - open transport, initialize, learn capabilities
* :meth:`list_tools` / :meth:`list_resources` / :meth:`list_prompts`
* :meth:`call_tool` - normalised to ``(ok, text, structured, is_error)``
* :meth:`ping` - health with latency
* :meth:`close` - always safe, always idempotent

Lifecycle note: the SDK's transports are async context managers, so a live
connection owns an :class:`contextlib.AsyncExitStack`.  ``connect`` and every
call happen on the caller's event loop; the manager serialises access per
connection because ``ClientSession`` is not re-entrant across tasks.

Secret refs in ``env`` / ``headers`` values (``env:VAR``, ``store:key``,
``file:…``) are resolved at connect time and never logged.
"""

from __future__ import annotations

import contextlib
import shlex
import time
from typing import Any

from unknown_agent.config.models import McpServerConfig
from unknown_agent.mcp.models import (
    McpCapabilities,
    McpHealth,
    McpPromptInfo,
    McpResourceInfo,
    McpToolInfo,
)
from unknown_agent.observability.logging import get_logger

log = get_logger("mcp.client")

__all__ = ["McpConnection", "McpConnectError", "mask_target", "resolve_secret_map"]


class McpConnectError(RuntimeError):
    """Connecting or initializing failed.  Message is user-facing."""


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute of ``names``.

    The SDK renamed its wire fields between major lines (1.x ``inputSchema`` /
    ``mimeType`` / ``isError`` → 2.x ``input_schema`` / ``mime_type`` /
    ``is_error``).  Reading both spellings keeps this client working against
    either installed SDK, which matters because ``mcp`` is an optional extra the
    user may have pinned for their own servers.
    """

    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def resolve_secret_map(values: dict[str, str]) -> dict[str, str]:
    """Resolve ``env:``/``store:``/``file:`` refs in a str->str mapping.

    Unresolvable refs resolve to "" rather than raising: a missing optional
    header must not stop a server that does not need it; the server itself will
    reject the request if the credential genuinely mattered, and that error
    names the server rather than the config loader.
    """

    from unknown_agent.config.secrets import resolve_secret

    resolved: dict[str, str] = {}
    for key, value in (values or {}).items():
        if isinstance(value, str) and value.partition(":")[0] in {"env", "store", "file", "keyring"}:
            try:
                resolved[key] = resolve_secret(value) or ""
            except Exception:  # noqa: BLE001 - a bad ref is a soft failure here
                resolved[key] = ""
        else:
            resolved[key] = value
    return resolved


def mask_target(config: McpServerConfig) -> str:
    """Human-facing 'where does this server live', with secrets masked."""

    if config.transport == "stdio":
        parts = [config.command or "?", *config.args]
        return " ".join(shlex.quote(part) for part in parts)
    return config.url or "?"


class McpConnection:
    """A live (or connectable) session with one MCP server."""

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self.name = name
        self.config = config
        self.capabilities: McpCapabilities | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any = None

    # -- lifecycle -----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> McpCapabilities:
        """Open the transport, initialize, and record capabilities."""

        if self._session is not None and self.capabilities is not None:
            return self.capabilities

        from unknown_agent.mcp import mcp_available

        available, why = mcp_available()
        if not available:
            raise McpConnectError(why)

        import anyio

        # NOTE on timeouts: the transports open anyio cancel scopes that must be
        # exited in strict nesting order by the same task, so ``fail_after`` may
        # NOT wrap the context entries (it would corrupt the scope stack on
        # timeout).  The deadline is therefore applied to ``initialize()`` alone
        # — a plain request/response — which is also where a hung server would
        # actually hang.  Transport-level connect timeouts belong to the
        # transports themselves (subprocess spawn is immediate; HTTP clients get
        # ``request_timeout_s``).
        stack = contextlib.AsyncExitStack()
        try:
            read, write = await self._open_transport(stack)
            session = await self._enter_session(stack, read, write)
            with anyio.fail_after(self.config.startup_timeout_s):
                result = await session.initialize()
        except TimeoutError:
            await stack.aclose()
            raise McpConnectError(
                f"server {self.name!r} did not initialize within {self.config.startup_timeout_s:.0f}s"
            ) from None
        except McpConnectError:
            await stack.aclose()
            raise
        except BaseException as exc:  # anyio may raise ExceptionGroups
            await stack.aclose()
            raise McpConnectError(f"could not connect to {self.name!r}: {_first_message(exc)}") from None

        self._stack = stack
        self._session = session
        self.capabilities = _capabilities_from(result)
        log.debug("mcp %s connected: %s", self.name, self.capabilities.server_name)
        return self.capabilities

    async def _open_transport(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        config = self.config
        if config.transport == "stdio":
            import os

            from mcp.client.stdio import StdioServerParameters, stdio_client

            env = {**os.environ, **resolve_secret_map(config.env)}
            params = StdioServerParameters(
                command=config.command or "",
                args=list(config.args),
                env=env,
                cwd=config.cwd,
            )
            return await stack.enter_async_context(stdio_client(params))

        headers = resolve_secret_map(config.headers)
        if config.transport == "http":
            import httpx

            from mcp.client.streamable_http import streamable_http_client

            client = httpx.AsyncClient(headers=headers, timeout=config.request_timeout_s, follow_redirects=True)
            stack.push_async_callback(client.aclose)
            streams = await stack.enter_async_context(
                streamable_http_client(config.url or "", http_client=client)
            )
            # TransportStreams is (read, write) possibly with extras appended.
            return streams[0], streams[1]

        if config.transport == "sse":
            from mcp.client.sse import sse_client

            streams = await stack.enter_async_context(
                sse_client(config.url or "", headers=headers, timeout=config.startup_timeout_s)
            )
            return streams[0], streams[1]

        raise McpConnectError(f"unknown transport {config.transport!r}")

    async def _enter_session(self, stack: contextlib.AsyncExitStack, read: Any, write: Any) -> Any:
        from mcp import ClientSession

        return await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=self.config.request_timeout_s)
        )

    async def close(self) -> None:
        session, stack = self._session, self._stack
        self._session = None
        self._stack = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()
        del session

    # -- inspection ----------------------------------------------------------
    async def list_tools(self) -> list[McpToolInfo]:
        session = self._require_session()
        result = await session.list_tools()
        tools: list[McpToolInfo] = []
        for tool in result.tools:
            annotations: dict[str, Any] = {}
            raw = getattr(tool, "annotations", None)
            if raw is not None:
                dump = getattr(raw, "model_dump", None)
                annotations = dump(exclude_none=True, by_alias=True) if callable(dump) else dict(raw)
            tools.append(
                McpToolInfo(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(_attr(tool, "input_schema", "inputSchema", default={}) or {}),
                    annotations=_normalise_annotations(annotations),
                )
            )
        return tools

    async def list_resources(self) -> list[McpResourceInfo]:
        session = self._require_session()
        result = await session.list_resources()
        return [
            McpResourceInfo(
                uri=str(item.uri),
                name=item.name or "",
                description=item.description or "",
                mime_type=str(_attr(item, "mime_type", "mimeType", default="") or ""),
            )
            for item in result.resources
        ]

    async def list_prompts(self) -> list[McpPromptInfo]:
        session = self._require_session()
        result = await session.list_prompts()
        prompts: list[McpPromptInfo] = []
        for item in result.prompts:
            arguments = [
                {"name": arg.name, "description": arg.description or "", "required": bool(arg.required)}
                for arg in (item.arguments or [])
            ]
            prompts.append(McpPromptInfo(name=item.name, description=item.description or "", arguments=arguments))
        return prompts

    async def read_resource(self, uri: str) -> tuple[str, str]:
        """Return ``(text, mime_type)`` for a resource; binary content is summarised."""

        session = self._require_session()
        result = await session.read_resource(uri)  # type: ignore[arg-type]
        for content in result.contents:
            text = getattr(content, "text", None)
            mime = str(_attr(content, "mime_type", "mimeType", default="") or "")
            if text is not None:
                return str(text), mime or "text/plain"
            blob = getattr(content, "blob", None)
            if blob is not None:
                return f"<binary resource: {len(blob)} base64 chars>", mime or "application/octet-stream"
        return "", ""

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> list[dict[str, str]]:
        """Return prompt messages as ``[{role, text}, …]``."""

        session = self._require_session()
        result = await session.get_prompt(name, arguments or {})
        messages: list[dict[str, str]] = []
        for message in result.messages:
            content = message.content
            text = getattr(content, "text", None)
            messages.append({"role": str(message.role), "text": str(text) if text is not None else str(content)})
        return messages

    # -- execution -----------------------------------------------------------
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Call a tool; return ``(ok, text, structured)``.

        ``isError`` on the MCP result maps to ``ok=False`` - a domain failure the
        agent should observe, not an exception.
        """

        session = self._require_session()
        result = await session.call_tool(name, arguments, read_timeout_seconds=self.config.request_timeout_s)
        is_error = bool(_attr(result, "is_error", "isError", default=False))
        texts: list[str] = []
        for content in getattr(result, "content", None) or []:
            text = getattr(content, "text", None)
            if text is not None:
                texts.append(str(text))
                continue
            data = getattr(content, "data", None)
            if data is not None:
                texts.append(f"<{getattr(content, 'type', 'binary')} content: {len(str(data))} chars>")
        structured = _attr(result, "structured_content", "structuredContent", default=None) or {}
        if not isinstance(structured, dict):
            structured = {"value": structured}
        return (not is_error), "\n".join(texts), structured

    async def ping(self) -> McpHealth:
        started = time.perf_counter()
        try:
            if self._session is None:
                await self.connect()
                # initialize already round-tripped; that latency is the answer.
                return McpHealth(ok=True, latency_ms=(time.perf_counter() - started) * 1000)
            await self._session.send_ping()
            return McpHealth(ok=True, latency_ms=(time.perf_counter() - started) * 1000)
        except BaseException as exc:  # noqa: BLE001 - health is a report, not a crash
            return McpHealth(ok=False, latency_ms=(time.perf_counter() - started) * 1000, error=_first_message(exc))

    # -- internals -----------------------------------------------------------
    def _require_session(self) -> Any:
        if self._session is None:
            raise McpConnectError(f"server {self.name!r} is not connected")
        return self._session


_ANNOTATION_ALIASES = {
    "read_only_hint": "readOnlyHint",
    "destructive_hint": "destructiveHint",
    "idempotent_hint": "idempotentHint",
    "open_world_hint": "openWorldHint",
}


def _normalise_annotations(annotations: dict[str, Any]) -> dict[str, Any]:
    """Spell every annotation the way the spec does (camelCase)."""

    return {_ANNOTATION_ALIASES.get(key, key): value for key, value in (annotations or {}).items()}


def _capabilities_from(result: Any) -> McpCapabilities:
    caps = getattr(result, "capabilities", None)
    info = _attr(result, "server_info", "serverInfo")
    tools = getattr(caps, "tools", None)
    resources = getattr(caps, "resources", None)
    prompts = getattr(caps, "prompts", None)
    return McpCapabilities(
        tools=tools is not None,
        resources=resources is not None,
        prompts=prompts is not None,
        logging=getattr(caps, "logging", None) is not None,
        completions=getattr(caps, "completions", None) is not None,
        tools_list_changed=bool(_attr(tools, "list_changed", "listChanged", default=False)) if tools else False,
        resources_subscribe=bool(getattr(resources, "subscribe", False)) if resources else False,
        server_name=getattr(info, "name", "") or "",
        server_version=getattr(info, "version", "") or "",
        protocol_version=str(_attr(result, "protocol_version", "protocolVersion", default="") or ""),
        instructions=getattr(result, "instructions", "") or "",
    )


def _first_message(exc: BaseException) -> str:
    """Unwrap ExceptionGroups (anyio task groups love them) to the useful message."""

    seen: BaseException = exc
    for _ in range(6):
        group = getattr(seen, "exceptions", None)
        if group:
            seen = group[0]
            continue
        break
    text = str(seen).strip() or seen.__class__.__name__
    return text if len(text) < 300 else text[:297] + "..."
