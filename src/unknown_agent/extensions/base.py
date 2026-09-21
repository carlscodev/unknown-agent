"""Plugin contract.

A plugin is any object exposing ``name``, ``version`` and an optional
``register(context)`` method.  Because capability registration happens through a
single context object, plugins can extend every part of the product without
importing internals or monkey-patching:

* tools            ``context.add_tool(...)``
* providers        ``context.add_provider(name, factory)``
* validation gates ``context.add_gate(...)``
* memory kinds     ``context.add_fact_kind("decision-record")``
* commands         ``context.add_command(name, callable, help_text)``

Everything is optional; a plugin that fails to load is reported and skipped so a
broken third-party package can never stop the agent from starting.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from unknown_agent.observability.logging import get_logger

log = get_logger("extensions.base")


@runtime_checkable
class Plugin(Protocol):
    name: str
    version: str

    def register(self, context: PluginContext) -> None:  # pragma: no cover - protocol
        ...


@dataclass(slots=True)
class PluginRecord:
    """What a loaded plugin contributed, for ``ua plugins list``."""

    name: str
    version: str = "0.0.0"
    source: str = ""
    path: str = ""
    tools: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    fact_kinds: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "path": self.path,
            "tools": self.tools,
            "providers": self.providers,
            "gates": self.gates,
            "commands": self.commands,
            "fact_kinds": self.fact_kinds,
            "error": self.error,
        }


@dataclass(slots=True)
class PluginContext:
    """The only API a plugin needs.  Registration is additive and logged."""

    settings: Any
    registry: Any = None
    providers: Any = None
    commands: dict[str, Callable[..., Any]] = field(default_factory=dict)
    gates: list[Any] = field(default_factory=list)
    fact_kinds: list[str] = field(default_factory=list)
    record: PluginRecord | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- registration ------------------------------------------------------
    def add_tool(self, tool: Any, *, overwrite: bool = False) -> None:
        if self.registry is None:
            raise RuntimeError("tool registry is not available in this context")
        self.registry.register(tool, overwrite=overwrite, source="plugin")
        if self.record is not None:
            self.record.tools.append(getattr(tool, "name", str(tool)))

    def add_provider(self, name: str, factory: Callable[..., Any], *, overwrite: bool = True) -> None:
        from unknown_agent.providers.registry import register_provider

        register_provider(name, factory, overwrite=overwrite)
        if self.record is not None:
            self.record.providers.append(name)

    def add_gate(self, gate: Any) -> None:
        self.gates.append(gate)
        if self.record is not None:
            self.record.gates.append(getattr(gate, "name", str(gate)))

    def add_fact_kind(self, kind: str) -> None:
        self.fact_kinds.append(kind)
        if self.record is not None:
            self.record.fact_kinds.append(kind)

    def add_command(self, name: str, handler: Callable[..., Any], *, help_text: str = "") -> None:
        self.commands[name] = handler
        if self.record is not None:
            self.record.commands.append(name)

    # -- convenience -------------------------------------------------------
    def config(self, section: str, default: Any = None) -> Any:
        """Read a plugin section from user config: ``[plugins.settings.<name>]``."""

        settings_map = getattr(self.settings, "plugins", None)
        extra = getattr(settings_map, "settings", None) or {}
        if isinstance(extra, dict):
            return extra.get(section, default)
        return default

    def log(self, message: str, **data: Any) -> None:
        log.info("[plugin:%s] %s", self.record.name if self.record else "?", message, extra=data)


class SimplePlugin:
    """Convenience base class for plugins that only register capabilities."""

    name = "unnamed-plugin"
    version = "0.0.0"

    def register(self, context: PluginContext) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


def load_object(path: str) -> Any:
    """Import ``module.path:object`` (or ``module.path.object``)."""

    import importlib

    if ":" in path:
        module_path, _, attribute = path.partition(":")
    else:
        module_path, _, attribute = path.rpartition(".")
    if not module_path or not attribute:
        raise ValueError(f"invalid plugin reference {path!r}; expected 'package.module:object'")
    module = importlib.import_module(module_path)
    return getattr(module, attribute)


__all__ = ["Plugin", "PluginContext", "PluginRecord", "SimplePlugin", "load_object"]
