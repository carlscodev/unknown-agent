"""Tool registry: discovery, lookup, schema export, plugin registration."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from unknown_agent.core.models import RiskLevel
from unknown_agent.errors import ConfigError
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import ToolSchema
from unknown_agent.tools.base import Tool, ToolNotFound

log = get_logger("tools.registry")


class ToolRegistry:
    """An ordered, deduplicated set of tools."""

    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        #: name -> human explanation for tools that exist but were filtered out
        #: (read-only sessions, disabled browser extras, disabled categories).
        self._withheld: dict[str, str] = {}
        for tool in tools or ():
            self.register(tool)

    # -- mutation ----------------------------------------------------------
    def register(self, tool: Tool, *, overwrite: bool = False, source: str = "builtin") -> None:
        name = tool.name.strip()
        if not name:
            raise ConfigError(f"Tool {type(tool).__name__} has no name.")
        if name in self._tools and not overwrite:
            raise ConfigError(
                f"Tool {name!r} is already registered.",
                hint="Pass overwrite=True when a plugin intentionally replaces a builtin tool.",
            )
        tool.metadata_source = source  # type: ignore[attr-defined]
        self._tools[name] = tool
        log.debug("registered tool %s (%s)", name, source)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def withhold(self, name: str, reason: str) -> None:
        """Remove a tool *and* remember why, so error messages can explain it."""

        if self.has(name):
            self._tools.pop(name, None)
        self._withheld[name] = reason

    def withholder_reason(self, name: str) -> str | None:
        return self._withheld.get(name)

    def extend(self, tools: Iterable[Tool], *, overwrite: bool = False, source: str = "plugin") -> None:
        for tool in tools:
            self.register(tool, overwrite=overwrite, source=source)

    # -- access ------------------------------------------------------------
    def has(self, name: str) -> bool:
        """True when a tool with this exact name is registered."""

        return (name or "").strip() in self._tools

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFound(name, self.names(), withheld_reason=self._withheld.get(name))
        return tool

    def maybe_get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def by_category(self) -> dict[str, list[Tool]]:
        grouped: dict[str, list[Tool]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.category, []).append(tool)
        return grouped

    def filter(self, *, categories: Iterable[str] | None = None, names: Iterable[str] | None = None, max_risk: RiskLevel | None = None, tags: Iterable[str] | None = None) -> list[Tool]:
        order = {RiskLevel.READ: 0, RiskLevel.WRITE: 1, RiskLevel.EXECUTE: 2, RiskLevel.NETWORK: 2, RiskLevel.DESTRUCTIVE: 3}
        selected = list(self._tools.values())
        if categories:
            wanted = {item.lower() for item in categories}
            selected = [tool for tool in selected if tool.category.lower() in wanted]
        if names:
            wanted_names = set(names)
            selected = [tool for tool in selected if tool.name in wanted_names]
        if max_risk is not None:
            ceiling = order[max_risk]
            selected = [tool for tool in selected if order[tool.risk] <= ceiling]
        if tags:
            wanted_tags = set(tags)
            selected = [tool for tool in selected if wanted_tags & set(tool.tags)]
        return selected

    def schemas(self, tools: Iterable[Tool] | None = None) -> list[ToolSchema]:
        selected = list(tools) if tools is not None else self.all()
        return [
            ToolSchema(name=tool.name, description=tool.description, parameters=tool.json_schema())
            for tool in selected
        ]

    def catalog(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    def describe(self, name: str) -> dict[str, Any]:
        return self.get(name).spec()

    def json_schema(self, name: str) -> dict[str, Any]:
        """Model-facing JSON schema for one tool."""

        tool = self.get(name)
        return {"name": tool.name, "description": tool.description, "parameters": tool.json_schema()}

    # -- introspection -----------------------------------------------------
    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())


__all__ = ["ToolRegistry"]
