"""``ua context`` - see exactly what the model is being told.

Prompt transparency is a product feature, not a debugging nicety: if the agent
behaves unexpectedly, the first question is "what did it know?".  These commands
answer that without reading source code.
"""

from __future__ import annotations

from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.context.builder import ContextBuilder, estimate_tokens
from unknown_agent.context.project import load_project_context
from unknown_agent.context.repo_map import build_repo_map
from unknown_agent.memory.procedures import ProcedureLibrary
from unknown_agent.memory.store import MemoryStore
from unknown_agent.tools.factory import build_default_registry
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table

app = typer.Typer(name="context", help="Inspect the context the agent builds (prompt transparency).", no_args_is_help=True)


def _builder(ctx: typer.Context) -> tuple[Runtime, ContextBuilder, object]:
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    root = Path(loaded.project_root or loaded.paths.cwd)
    project = load_project_context(root, loaded.settings)
    memory = MemoryStore(loaded.paths.project_dir / "memory" / "memory.db", fts_enabled=loaded.settings.memory.fts_enabled)
    procedures = ProcedureLibrary(loaded.paths.project_dir / "procedures", loaded.paths.procedures_dir)
    builder = ContextBuilder(loaded.settings, project=project, memory=memory, procedures=procedures)
    return runtime, builder, project


@app.command("show")
def show(
    ctx: typer.Context,
    task: str = typer.Option(..., "--task", help="Task to preview the context for."),
    full: bool = typer.Option(False, "--full", help="Print the entire system prompt."),
) -> None:
    """Preview the system prompt and context block for a task."""

    runtime, builder, project = _builder(ctx)
    registry = build_default_registry(runtime.settings)
    message = builder.build_system_message(tool_specs=[tool.spec() for tool in registry.all()], task=task)
    if runtime.is_machine:
        runtime.emit_json({"system_prompt": message.content, "chars": len(message.content), "tokens": estimate_tokens(message.content)})
        return
    if full:
        output.raw(message.content)
        return
    preview = "\n".join(message.content.splitlines()[:60])
    output.print(preview)
    output.print(f"\n[ua.muted]{len(message.content):,} characters ≈ {estimate_tokens(message.content):,} tokens (use --full to print everything)[/ua.muted]")


@app.command("tokens")
def tokens(ctx: typer.Context, task: str = typer.Option(..., "--task", help="Task to estimate for.")) -> None:
    """Token budget breakdown for a task."""

    runtime, builder, project = _builder(ctx)
    registry = build_default_registry(runtime.settings)
    specs = [tool.spec() for tool in registry.all()]
    _messages, budget = builder.build_plan_messages(task=task, tool_specs=specs)
    repo_map = builder.repo_map_block()
    budget.repo_map_tokens = estimate_tokens(repo_map)
    recall = builder.recall(task)
    if runtime.is_machine:
        runtime.emit_json(budget.as_dict())
        return
    rows = [(key, f"{value:,}") for key, value in budget.as_dict().items()]
    output.print(kv_table(rows, title="context budget (estimated)"))
    if recall:
        output.print("\n[ua.muted]facts that would be recalled:[/ua.muted]")
        for fact in recall:
            output.print(f"  #{fact['id']} ({fact['kind']}) {fact['text'][:90]}")


@app.command("repo-map")
def repo_map(
    ctx: typer.Context,
    refresh: bool = typer.Option(False, "--refresh", help="Rebuild the cached map."),
    paths_only: bool = typer.Option(False, "--paths", help="Print just the file list."),
) -> None:
    """The repository map the agent uses for structural awareness."""

    runtime = ctx.obj
    loaded = runtime.load()
    root = Path(loaded.project_root or loaded.paths.cwd)
    repo = build_repo_map(root, cache_dir=loaded.paths.project_cache_dir, refresh=refresh)
    if runtime.is_machine:
        runtime.emit_json(repo.to_dict())
        return
    output.print(repo.render_paths() if paths_only else repo.render())


@app.command("procedures")
def procedures(ctx: typer.Context, task: str = typer.Option(..., "--task", help="Task to match procedures against.")) -> None:
    """Which procedures would activate for a task."""

    runtime, builder, _project = _builder(ctx)
    matched = builder.matching_procedures(task)
    if runtime.is_machine:
        runtime.emit_json({"procedures": [item.to_dict() for item in matched]})
        return
    if not matched:
        output.print("[ua.muted]no procedures match this task[/ua.muted]")
        return
    output.print(data_table(["name", "scope", "triggers"], [(item.name, item.scope, ", ".join(item.triggers) or "-") for item in matched], title="active procedures"))


__all__ = ["app"]
