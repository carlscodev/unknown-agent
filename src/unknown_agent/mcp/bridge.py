"""MCP tools as first-class :class:`unknown_agent.tools.base.Tool` objects.

The bridge is where an *external* capability becomes a *product* capability, so
this is where the safety decisions concentrate:

* **Naming.**  Every proxied tool is ``mcp.<server>.<tool>``.  The agent, the
  journal and the audit trail always know where a call went; two servers with a
  ``search`` tool never collide; and a malicious server cannot shadow a builtin
  (``write_file`` from a server becomes ``mcp.evil.write_file``, visibly).

* **Risk.**  MCP annotations (readOnlyHint / destructiveHint) are *hints from an
  untrusted party*.  They are mapped onto the product risk ladder but only ever
  upward-biased: an un-annotated tool is ``execute`` risk, an explicitly
  destructive one is ``destructive``.  A hint can never lower risk below
  ``write`` unless the server's trust level is ``full``.

* **Trust.**  ``sandboxed`` servers get every call confirmed (the proxy sets
  ``risk >= execute`` so the policy engine prompts); ``blocked`` servers never
  reach the bridge at all (the manager filters them).

* **Normalisation.**  Results come back as :class:`ToolResult` with the text
  content in ``output``, ``structuredContent`` in ``data``, and the MCP
  ``isError`` flag mapped to ``ok`` - so the loop, renderers and the journal
  treat MCP output exactly like builtin output.
"""

from __future__ import annotations

import json
from typing import Any

from unknown_agent.config.models import McpServerConfig
from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.mcp.models import McpToolInfo
from unknown_agent.observability.logging import get_logger
from unknown_agent.tools.base import Tool, ToolContext

log = get_logger("mcp.bridge")

__all__ = ["McpProxyTool", "proxy_name", "risk_for", "build_proxies"]

#: Category every proxied tool carries; `ua tools list` groups by it.
MCP_CATEGORY = "mcp"


def proxy_name(server: str, tool: str) -> str:
    return f"mcp.{server}.{tool}"


def risk_for(info: McpToolInfo, trust: str) -> RiskLevel:
    """Map server hints + trust onto the product risk ladder (upward-biased)."""

    if trust == "sandboxed":
        # Every call from a sandboxed server should hit the confirmation path.
        return RiskLevel.DESTRUCTIVE if info.destructive_hint else RiskLevel.EXECUTE
    if info.read_only_hint:
        # Only fully-trusted servers get the friction-free read lane.
        return RiskLevel.READ if trust == "full" else RiskLevel.WRITE
    if info.destructive_hint:
        return RiskLevel.DESTRUCTIVE
    return RiskLevel.EXECUTE


class McpProxyTool(Tool):
    """One MCP tool, wearing the product's Tool contract.

    Unlike builtin tools (whose identity is class-level), a proxy's identity is
    instance-level - so ``json_schema``/``spec``/``validate_arguments`` are
    overridden as instance methods.  Argument validation is delegated to the
    server's own JSON schema: we pass arguments through unmodified (the server
    is the authority on its own contract) after a shallow required-keys check
    that produces friendlier errors than a remote validation failure.
    """

    category = MCP_CATEGORY
    concurrency_safe = False  # ClientSession serialises anyway; be honest about it

    def __init__(self, server: str, config: McpServerConfig, info: McpToolInfo) -> None:
        self.server = server
        self.server_config = config
        self.info = info
        self.name = proxy_name(server, info.name)
        self.description = _describe(server, config, info)
        self.risk = risk_for(info, config.trust)
        self.timeout_s = float(config.request_timeout_s)
        self.tags = ("mcp", f"mcp:{server}", *((f"mcp-group:{config.group}",) if config.group else ()))

    # -- schema (instance-level) --------------------------------------------
    def json_schema(self) -> dict[str, Any]:  # type: ignore[override]
        schema = dict(self.info.input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema

    def spec(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk": self.risk.value,
            "parameters": dict(self.json_schema().get("properties", {})),
            "required": list(self.json_schema().get("required", [])),
            "examples": [],
            "timeout_s": self.timeout_s,
            "requires_extra": "mcp",
            "tags": list(self.tags),
            "mcp": {
                "server": self.server,
                "tool": self.info.name,
                "trust": self.server_config.trust,
                "annotations": dict(self.info.annotations),
            },
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:  # type: ignore[override]
        problems: list[str] = []
        cleaned = dict(arguments or {})
        for key in self.json_schema().get("required", []) or []:
            if key not in cleaned:
                problems.append(f"missing required argument {key!r}")
        return cleaned, problems

    def summarise_call(self, arguments: dict[str, Any]) -> str:  # type: ignore[override]
        rendered = json.dumps(arguments, ensure_ascii=False, default=str)
        if len(rendered) > 120:
            rendered = rendered[:117] + "..."
        return f"{self.name}({rendered})"

    # -- execution -----------------------------------------------------------
    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        manager = (ctx.extra or {}).get("mcp_manager")
        if manager is None:
            return ToolResult.failure(
                "No MCP manager on this session.",
                metadata={"server": self.server},
            )
        try:
            ok, text, structured = await manager.call_tool(self.server, self.info.name, kwargs)
        except Exception as exc:  # noqa: BLE001 - transport failures are domain failures here
            return ToolResult.failure(
                f"MCP server {self.server!r} failed: {exc}",
                metadata={"server": self.server, "tool": self.info.name},
            )
        metadata = {"server": self.server, "tool": self.info.name, "trust": self.server_config.trust}
        if self.server_config.trust == "sandboxed":
            # Content from a sandboxed server is labelled so the model (and the
            # journal) knows it is untrusted input, not the product speaking.
            text = f"[untrusted content from mcp server '{self.server}']\n{text}" if text else text
        if ok:
            return ToolResult(ok=True, output=text, data=dict(structured), metadata=metadata)
        return ToolResult(ok=False, output=text, error=text or f"{self.info.name} reported an error", data=dict(structured), metadata=metadata)


def _describe(server: str, config: McpServerConfig, info: McpToolInfo) -> str:
    base = (info.description or "").strip() or f"{info.name} (no description provided)"
    suffix = f" [via MCP server '{server}'"
    if config.trust != "full":
        suffix += f", trust: {config.trust}"
    suffix += "]"
    return base + suffix


def build_proxies(selected: list[tuple[str, McpServerConfig, McpToolInfo]]) -> list[McpProxyTool]:
    """Turn the manager's selection into registerable tools, skipping collisions."""

    proxies: list[McpProxyTool] = []
    seen: set[str] = set()
    for server, config, info in selected:
        name = proxy_name(server, info.name)
        if name in seen:
            log.warning("duplicate MCP tool %s ignored", name)
            continue
        seen.add(name)
        proxies.append(McpProxyTool(server, config, info))
    return proxies
