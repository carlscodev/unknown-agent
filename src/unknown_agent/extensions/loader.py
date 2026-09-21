"""Plugin discovery and loading.

Three discovery channels, in order:

1. **entry points** - ``unknown_agent.plugins`` groups from installed
   distributions (the supported way to ship a plugin on PyPI),
2. **user directory** - ``~/.config/unknown-agent/plugins/*.py``,
3. **project directory** - ``<repo>/.unknown/plugins/*.py``.

Load failures are isolated: a broken plugin is recorded with its error, reported
by ``ua plugins list``, and the agent starts anyway.  Plugins can be disabled by
name through ``[plugins] disabled = ["name"]``.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Any

from unknown_agent.errors import PluginError
from unknown_agent.extensions.base import PluginContext, PluginRecord, SimplePlugin, load_object
from unknown_agent.observability.logging import get_logger

log = get_logger("extensions.loader")

ENTRY_POINT_GROUP = "unknown_agent.plugins"


class PluginManager:
    """Discovers, loads and reports plugins."""

    def __init__(
        self,
        *,
        allow_entry_points: bool = True,
        user_dir: Path | None = None,
        project_dir: Path | None = None,
        disabled: list[str] | None = None,
    ) -> None:
        self.allow_entry_points = allow_entry_points
        self.user_dir = Path(user_dir) if user_dir else None
        self.project_dir = Path(project_dir) if project_dir else None
        self.disabled = {name.lower() for name in (disabled or [])}
        self.records: list[PluginRecord] = []
        self.context: PluginContext | None = None

    # ------------------------------------------------------------------ #
    def load(self, *, registry: Any = None, settings: Any = None) -> PluginContext:
        context = PluginContext(settings=settings, registry=registry)
        self.context = context
        for candidate in self.discover():
            if candidate["name"].lower() in self.disabled:
                log.info("plugin %s is disabled by configuration", candidate["name"])
                continue
            self._load_one(candidate, context)
        if self.records:
            log.debug("loaded %s plugin(s)", len([record for record in self.records if not record.error]))
        return context

    def discover(self) -> list[dict[str, str]]:
        """Return candidate plugins without importing them."""

        candidates: list[dict[str, str]] = []
        if self.allow_entry_points:
            try:
                from importlib.metadata import entry_points

                found = entry_points()
                group = found.select(group=ENTRY_POINT_GROUP) if hasattr(found, "select") else found.get(ENTRY_POINT_GROUP, [])  # type: ignore[union-attr]
                for entry in group:
                    candidates.append({"name": entry.name, "source": "entry-point", "path": entry.value})
            except Exception:  # pragma: no cover - metadata edge cases
                log.debug("entry point discovery failed", exc_info=True)
        for directory, source in ((self.user_dir, "user"), (self.project_dir, "project")):
            if directory is None or not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                candidates.append({"name": path.stem, "source": source, "path": str(path)})
        return candidates

    def _load_one(self, candidate: dict[str, str], context: PluginContext) -> None:
        record = PluginRecord(name=candidate["name"], source=candidate["source"], path=candidate["path"])
        context.record = record
        try:
            plugin_object = self._import(candidate)
            instance = self._instantiate(plugin_object)
            record.name = getattr(instance, "name", record.name)
            record.version = getattr(instance, "version", "0.0.0")
            register = getattr(instance, "register", None)
            if register is None:
                raise PluginError(f"plugin {record.name!r} has no register() method")
            register(context)
        except Exception as exc:  # noqa: BLE001 - never let a plugin stop the agent
            record.error = f"{type(exc).__name__}: {exc}"
            log.warning("plugin %s failed to load: %s", candidate["name"], record.error)
            log.debug("plugin traceback:\n%s", traceback.format_exc())
        finally:
            self.records.append(record)
            context.record = None

    def _import(self, candidate: dict[str, str]) -> Any:
        if candidate["source"] == "entry-point":
            try:
                from importlib.metadata import entry_points

                found = entry_points()
                group = found.select(group=ENTRY_POINT_GROUP) if hasattr(found, "select") else found.get(ENTRY_POINT_GROUP, [])  # type: ignore[union-attr]
                for entry in group:
                    if entry.name == candidate["name"]:
                        return entry.load()
            except Exception as exc:  # pragma: no cover
                raise PluginError(f"could not load entry point {candidate['path']}: {exc}") from exc
            return load_object(candidate["path"])
        path = Path(candidate["path"])
        module_name = f"unknown_agent_plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"cannot import plugin file {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return getattr(module, "PLUGIN", None) or getattr(module, "plugin", None) or module

    @staticmethod
    def _instantiate(candidate: Any) -> Any:
        if isinstance(candidate, type):
            return candidate()
        if hasattr(candidate, "register"):
            return candidate
        if hasattr(candidate, "PLUGIN"):
            return PluginManager._instantiate(candidate.PLUGIN)
        if isinstance(candidate, SimplePlugin):
            return candidate
        raise PluginError(f"plugin object {candidate!r} does not look like a plugin")

    # ------------------------------------------------------------------ #
    def summary(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.records]

    @property
    def loaded(self) -> list[PluginRecord]:
        return [record for record in self.records if not record.error]

    @property
    def failed(self) -> list[PluginRecord]:
        return [record for record in self.records if record.error]


def write_plugin_skeleton(path: Path, name: str) -> Path:
    """Create a working plugin file - the ``ua plugins new`` command."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    slug = name.strip().replace(" ", "-").lower()
    path.write_text(
        f'''"""Unknown Agent plugin: {name}.

Registered capabilities show up in `ua tools list`, `ua plugins list` and the
agent's tool catalog automatically.
"""

from __future__ import annotations

from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext


class ExampleTool(Tool):
    name = "{slug.replace('-', '_')}_tool"
    description = "Describe precisely when the agent should use this tool."
    category = "custom"
    risk = RiskLevel.READ
    tags = ("plugin",)
    parameters = (
        ParameterSpec("query", "string", "What to look up.", required=True),
    )

    async def run(self, ctx: ToolContext, query: str) -> ToolResult:
        ctx.emit("observation", f"{slug} tool called with {{query}}")
        return ToolResult.success(f"replace this body; received query={{query}}", query=query)


class Plugin:
    name = "{slug}"
    version = "0.1.0"

    def register(self, context: Any) -> None:
        context.add_tool(ExampleTool())
        context.add_command("hello", lambda: print("hello from {slug}"), help_text="Print a greeting.")
        # context.add_provider("my-backend", my_factory)
        # context.add_gate(MyGate())


PLUGIN = Plugin()
''',
        encoding="utf-8",
    )
    return path


__all__ = ["PluginManager", "write_plugin_skeleton", "ENTRY_POINT_GROUP"]
