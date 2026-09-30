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

from collections.abc import Callable
from dataclasses import dataclass, field
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
    hooks: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    fact_kinds: list[str] = field(default_factory=list)
    manifest_verified: bool = False
    publisher: str = ""
    key_id: str = ""
    permissions: list[str] = field(default_factory=list)
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
            "hooks": self.hooks,
            "commands": self.commands,
            "fact_kinds": self.fact_kinds,
            "manifest_verified": self.manifest_verified,
            "publisher": self.publisher,
            "key_id": self.key_id,
            "permissions": self.permissions,
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
    manifest: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    _pending_tools: list[tuple[Any, bool]] = field(default_factory=list)
    _pending_providers: list[tuple[str, Callable[..., Any], bool]] = field(default_factory=list)
    _pending_gates: list[Any] = field(default_factory=list)
    _pending_hooks: list[tuple[Any, Any]] = field(default_factory=list)
    _pending_fact_kinds: list[str] = field(default_factory=list)
    _pending_commands: list[tuple[str, Callable[..., Any], str]] = field(default_factory=list)

    # -- registration ------------------------------------------------------
    def begin_registration(self, record: PluginRecord, manifest: Any = None) -> None:
        self.discard_registration()
        self.record = record
        self.manifest = manifest

    def discard_registration(self) -> None:
        self._pending_tools.clear()
        self._pending_providers.clear()
        self._pending_gates.clear()
        self._pending_hooks.clear()
        self._pending_fact_kinds.clear()
        self._pending_commands.clear()
        self.record = None
        self.manifest = None

    def add_tool(self, tool: Any, *, overwrite: bool = False) -> None:
        if self.registry is None:
            raise RuntimeError("tool registry is not available in this context")
        name = getattr(tool, "name", "")
        risk = getattr(getattr(tool, "risk", None), "value", "")
        self._authorise("tool", name, "tools.register", risk=risk)
        from unknown_agent.extensions.manifest import required_tool_permission

        self._require_permission(required_tool_permission(risk))
        self._pending_tools.append((tool, overwrite))

    def add_provider(self, name: str, factory: Callable[..., Any], *, overwrite: bool = True) -> None:
        self._authorise("provider", name, "providers.register")
        self._pending_providers.append((name, factory, overwrite))

    def add_gate(self, gate: Any) -> None:
        name = getattr(gate, "name", str(gate))
        self._authorise("gate", name, "gates.register")
        self._pending_gates.append(gate)

    def add_hook(self, spec: Any, handler: Any) -> None:
        name = getattr(spec, "name", "")
        self._authorise("hook", name, "hooks.register")
        self._pending_hooks.append((spec, handler))

    def add_fact_kind(self, kind: str) -> None:
        self._authorise("fact_kind", kind, "memory.extend")
        self._pending_fact_kinds.append(kind)

    def add_command(self, name: str, handler: Callable[..., Any], *, help_text: str = "") -> None:
        self._authorise("command", name, "commands.register")
        self._pending_commands.append((name, handler, help_text))

    def commit_registration(self) -> None:
        """Publish a fully validated registration batch without partial tools."""

        if self.record is None:
            raise RuntimeError("no plugin registration is active")
        if self.registry is not None:
            names = [getattr(tool, "name", "") for tool, _ in self._pending_tools]
            if len(names) != len(set(names)):
                raise RuntimeError("plugin attempted to register duplicate tool names")
            for tool, overwrite in self._pending_tools:
                existing = self.registry.maybe_get(tool.name)
                if existing is not None and not overwrite:
                    raise RuntimeError(f"tool {tool.name!r} is already registered")
        registered: list[tuple[str, Any | None]] = []
        registered_hooks: list[str] = []
        try:
            for tool, overwrite in self._pending_tools:
                previous = self.registry.maybe_get(tool.name) if self.registry is not None else None
                self.registry.register(tool, overwrite=overwrite, source="plugin")
                registered.append((tool.name, previous))
            from unknown_agent.providers.registry import register_provider

            for name, factory, overwrite in self._pending_providers:
                register_provider(name, factory, overwrite=overwrite)
            hook_bus = self.extra.get("hook_bus")
            if self._pending_hooks and hook_bus is None:
                raise RuntimeError("hook bus is not available in this plugin context")
            for spec, handler in self._pending_hooks:
                assert hook_bus is not None
                hook_bus.register(spec, handler)
                registered_hooks.append(spec.name)
            self.gates.extend(self._pending_gates)
            self.fact_kinds.extend(self._pending_fact_kinds)
            for name, handler, _help_text in self._pending_commands:
                self.commands[name] = handler
        except Exception:
            hook_bus = self.extra.get("hook_bus")
            if hook_bus is not None:
                for name in reversed(registered_hooks):
                    hook_bus.unregister(name)
            if self.registry is not None:
                for name, previous in reversed(registered):
                    self.registry.unregister(name)
                    if previous is not None:
                        self.registry.register(previous, overwrite=True, source="restored")
            raise
        self.record.tools.extend(name for name, _previous in registered)
        self.record.providers.extend(name for name, _factory, _overwrite in self._pending_providers)
        self.record.gates.extend(getattr(item, "name", str(item)) for item in self._pending_gates)
        self.record.hooks.extend(spec.name for spec, _handler in self._pending_hooks)
        self.record.fact_kinds.extend(self._pending_fact_kinds)
        self.record.commands.extend(name for name, _handler, _help in self._pending_commands)
        self._pending_tools.clear()
        self._pending_providers.clear()
        self._pending_gates.clear()
        self._pending_hooks.clear()
        self._pending_fact_kinds.clear()
        self._pending_commands.clear()

    def _authorise(self, kind: str, name: str, permission: str, *, risk: str = "") -> None:
        if self.manifest is None:
            return
        self._require_permission(permission)
        declaration = self.manifest.declares(kind, name)
        if declaration is None:
            raise PermissionError(f"capability {kind}:{name} is not declared in the signed manifest")
        if kind == "tool" and declaration.risk != risk:
            raise PermissionError(
                f"tool {name!r} declares risk {declaration.risk!r} but registered as {risk!r}"
            )

    def _require_permission(self, permission: str) -> None:
        if self.manifest is not None and permission not in self.manifest.permissions:
            raise PermissionError(f"manifest does not grant permission {permission!r}")

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
