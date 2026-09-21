"""``ua inspect`` - repository intelligence without spending a token."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.context.project import load_project_context
from unknown_agent.context.repo_map import build_repo_map
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table, summary_panel

app = typer.Typer(
    name="inspect",
    help="Understand a repository: stack, commands, entry points, layout, risks.",
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _context(ctx: typer.Context) -> tuple[Runtime, object]:
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    root = Path(loaded.project_root or loaded.paths.cwd)
    project = load_project_context(root, loaded.settings)
    return runtime, project


@app.callback(invoke_without_command=True)
def inspect_all(
    ctx: typer.Context,
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the repo-map cache."),
) -> None:
    """Full report: identity, stack, commands, layout, risks."""

    runtime, project = _context(ctx)
    if runtime.is_machine:
        runtime.emit_json(project.to_dict())
        return
    output.blank()
    output.print(summary_panel("project", [("name", project.name), ("kind", project.kind), ("root", project.root)]))
    output.blank()
    output.print(project.render())
    output.blank()


@app.command("summary")
def summary(ctx: typer.Context) -> None:
    """One-screen summary (what the agent sees first)."""

    runtime, project = _context(ctx)
    if runtime.is_machine:
        runtime.emit_json(project.to_dict())
        return
    output.print(project.prompt_summary())


@app.command("commands")
def commands(ctx: typer.Context) -> None:
    """Detected dev/test/build/lint commands and where they came from."""

    runtime, project = _context(ctx)
    rows = [(name, command) for name, command in sorted((project.commands or {}).items())]
    if runtime.is_machine:
        runtime.emit_json({"commands": dict(project.commands or {}), "package_manager": project.package_manager})
        return
    if not rows:
        output.print("[ua.warn]No commands detected.[/ua.warn] Declare them in .unknown/config.toml under [project.commands].")
        return
    output.print(data_table(["command", "invocation"], rows, title=f"commands ({project.package_manager or 'unknown package manager'})"))


@app.command("tree")
def tree(
    ctx: typer.Context,
    path: str = typer.Option(".", "--path", help="Subdirectory to start from."),
    depth: int = typer.Option(3, "--depth", help="Maximum depth."),
    files: bool = typer.Option(True, "--files/--dirs-only", help="Include files."),
) -> None:
    """Repository tree, ignoring dependencies and build output."""

    runtime, project = _context(ctx)
    repo_map = build_repo_map(project.root, cache_dir=project.root / ".unknown" / "cache")
    if runtime.is_machine:
        runtime.emit_json(repo_map.to_dict())
        return
    if files:
        output.print(repo_map.render(max_files=200))
    else:
        for directory, count in sorted(repo_map.directories.items(), key=lambda item: -item[1])[:40]:
            output.print(f"{directory}/ ({count} files)")


@app.command("risks")
def risks(ctx: typer.Context) -> None:
    """Risky areas: secrets, missing lockfiles, no tests, monorepo hazards."""

    runtime, project = _context(ctx)
    if runtime.is_machine:
        runtime.emit_json({"risks": project.risks})
        return
    if not project.risks:
        output.print("[ua.ok]No structural risks detected.[/ua.ok]")
        return
    for risk in project.risks:
        output.print(f"[ua.warn]![/ua.warn] {risk}")


@app.command("entrypoints")
def entrypoints(ctx: typer.Context) -> None:
    """Entry points and guidance files the agent uses for orientation."""

    runtime, project = _context(ctx)
    payload = {"entry_points": project.entry_points, "pointers": list(project.pointers), "conventions": project.conventions}
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(kv_table([("entry points", project.entry_points or "none"), ("guidance files", list(project.pointers) or "none"), ("tooling", project.conventions or "none")]))


__all__ = ["app"]
