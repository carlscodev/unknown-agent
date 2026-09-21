"""``ua tools`` - the tool catalog, and direct invocation for scripting.

Direct invocation is what makes the tool layer reusable outside the agent loop:
``ua tools call read_file --arg path=README.md`` is a scriptable, policy-checked
operation with the same guarantees the agent gets (path containment, denylist,
auditing).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.core.models import ToolCall
from unknown_agent.errors import ExitCode, UsageError
from unknown_agent.tools.base import ToolContext
from unknown_agent.tools.executor import ToolExecutor
from unknown_agent.tools.factory import build_default_registry, describe_registry
from unknown_agent.tools.policy import PolicyEngine
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table, tool_card

app = typer.Typer(name="tools", help="List, describe and call tools directly.", no_args_is_help=True)


def _registry(ctx: typer.Context):
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    return runtime, loaded, build_default_registry(loaded.settings)


@app.command("list")
def list_tools(
    ctx: typer.Context,
    category: str | None = typer.Option(None, "--category", help="Filter by category."),
    risk: str | None = typer.Option(None, "--risk", help="Maximum risk level: read|write|execute|network|destructive."),
    tag: str | None = typer.Option(None, "--tag", help="Filter by tag."),
) -> None:
    """List every available tool."""

    runtime, _loaded, registry = _registry(ctx)
    from unknown_agent.core.models import RiskLevel

    tools = registry.all()
    if category:
        tools = [tool for tool in tools if tool.category == category]
    if tag:
        tools = [tool for tool in tools if tag in tool.tags]
    if risk:
        try:
            ceiling = RiskLevel(risk)
        except ValueError as exc:
            raise UsageError(f"Unknown risk level {risk!r}.", hint="Valid: read, write, execute, network, destructive") from exc
        order = {RiskLevel.READ: 0, RiskLevel.WRITE: 1, RiskLevel.EXECUTE: 2, RiskLevel.NETWORK: 2, RiskLevel.DESTRUCTIVE: 3}
        tools = [tool for tool in tools if order[tool.risk] <= order[ceiling]]

    if runtime.is_machine:
        runtime.emit_json({"count": len(tools), "tools": [tool.spec() for tool in tools]})
        return
    rows = [(tool.name, tool.category, tool.risk.value, ", ".join(tool.tags) or "-", tool.description.splitlines()[0][:70]) for tool in tools]
    output.print(data_table(["tool", "category", "risk", "tags", "description"], rows, title=f"tools ({len(tools)})"))


@app.command("describe")
def describe(ctx: typer.Context, name: str = typer.Argument(..., help="Tool name.")) -> None:
    """Show a tool's schema, risk level and examples."""

    runtime, _loaded, registry = _registry(ctx)
    spec = registry.describe(name)
    if runtime.is_machine:
        runtime.emit_json(spec)
        return
    output.blank()
    output.print(f"[ua.tool.name]{spec['name']}[/ua.tool.name]  [ua.muted]({spec['category']}, risk: {spec['risk']})[/ua.muted]")
    output.print(f"\n{spec['description']}\n")
    rows = []
    for param, schema in (spec["parameters"] or {}).items():
        required = "yes" if param in (spec["required"] or []) else "no"
        rows.append((param, schema.get("type", "string"), required, schema.get("description", "")[:60]))
    if rows:
        output.print(data_table(["argument", "type", "required", "description"], rows, title="arguments"))
    if spec["examples"]:
        output.print("\n[ua.muted]examples[/ua.muted]")
        for example in spec["examples"]:
            output.print(f"  {example}")


@app.command("schema")
def schema(ctx: typer.Context, out: Path | None = typer.Option(None, "--out", help="Write the JSON schemas to a file.")) -> None:
    """Export model-facing JSON schemas for every tool."""

    runtime, _loaded, registry = _registry(ctx)
    payload = {"tools": [{"name": tool.name, "description": tool.description, "parameters": tool.json_schema()} for tool in registry.all()]}
    if out:
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        output.print(f"[ua.ok]wrote[/ua.ok] {out}")
        return
    runtime.emit_json(payload)


@app.command("call")
def call(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Tool name."),
    arg: list[str] = typer.Option(None, "--arg", help="key=value argument (repeatable)."),
    json_args: str | None = typer.Option(None, "--json-args", help="Arguments as a JSON object."),
    cwd: Path | None = typer.Option(None, "--cwd", help="Project root for path resolution."),
) -> None:
    """Call a tool directly (policy-checked, audited).

    Examples:
      ua tools call read_file --arg path=src/app.py
      ua tools call search_code --arg pattern="TODO" --arg glob="*.py"
      ua tools call shell_run --arg command="git status --short"
      ua tools call run_gates --json-args '{"gates": ["tests"]}'
    """
    runtime, loaded, registry = _registry(ctx)
    arguments: dict[str, object] = {}
    if json_args:
        try:
            parsed = json.loads(json_args)
        except json.JSONDecodeError as exc:
            raise UsageError(f"--json-args must be valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise UsageError("--json-args must be a JSON object.")
        arguments.update(parsed)
    for pair in arg or []:
        if "=" not in pair:
            raise UsageError(f"--arg must be key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        arguments[key.strip()] = value

    project_root = Path(loaded.project_root or loaded.paths.cwd)
    from unknown_agent.context.project import load_project_context
    from unknown_agent.memory.store import MemoryStore
    from unknown_agent.observability.events import EventBus

    tool_context = ToolContext(
        settings=loaded.settings,
        paths=loaded.paths,
        project_root=project_root,
        events=EventBus(),
        project=load_project_context(project_root, loaded.settings),
        prompter=runtime.prompter,
        run_id="cli",
        artifacts_dir=loaded.paths.artifacts_dir,
        backups_dir=loaded.paths.backups_dir,
        cache_dir=loaded.paths.project_cache_dir,
    )
    store = MemoryStore(loaded.paths.project_dir / "memory" / "memory.db", fts_enabled=loaded.settings.memory.fts_enabled)
    tool_context.memory = store
    policy = PolicyEngine(loaded.settings, project_root=project_root, prompter=runtime.prompter)
    executor = ToolExecutor(registry=registry, settings=loaded.settings, context=tool_context, policy=policy)

    try:
        result = asyncio.run(executor.execute(ToolCall.new(name, arguments)))
    finally:
        store.close()

    if runtime.is_machine:
        runtime.emit_json({"tool": name, "arguments": arguments, "ok": result.ok, "output": result.output, "error": result.error, "data": result.data, "duration_ms": result.duration_ms})
    else:
        output.blank()
        tool = registry.maybe_get(name)
        output.print(
            tool_card(
                name,
                arguments,
                result=result.render(max_chars=8000),
                ok=result.ok,
                duration_s=result.duration_ms / 1000,
                risk=tool.risk.value if tool else "unknown",
            )
        )
    raise typer.Exit(ExitCode.OK if result.ok else ExitCode.TOOL)


@app.command("categories")
def categories(ctx: typer.Context) -> None:
    """List tool categories with counts."""

    runtime, _loaded, registry = _registry(ctx)
    grouped = {name: [tool.name for tool in tools] for name, tools in registry.by_category().items()}
    if runtime.is_machine:
        runtime.emit_json({"categories": grouped})
        return
    output.print(kv_table([(name, f"{len(names)}: {', '.join(names[:8])}") for name, names in sorted(grouped.items())], title="categories"))


@app.command("stats")
def stats(ctx: typer.Context) -> None:
    """Inventory summary (also used by docs generation)."""

    runtime, _loaded, registry = _registry(ctx)
    payload = describe_registry(registry)
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(kv_table([("tools", payload["count"]), ("categories", list(payload["categories"]))], title="tool inventory"))


__all__ = ["app"]
