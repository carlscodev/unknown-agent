"""``ua plugins`` - discover, scaffold and audit extensions."""

from __future__ import annotations

from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.extensions.loader import ENTRY_POINT_GROUP, PluginManager, write_plugin_skeleton
from unknown_agent.tools.factory import build_default_registry
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table

app = typer.Typer(name="plugins", help="Discover, scaffold and inspect plugins.", no_args_is_help=True)


def _manager(ctx: typer.Context, *, registry=None) -> tuple[Runtime, PluginManager, list]:
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    tools_registry = registry or build_default_registry(loaded.settings)
    manager = PluginManager(
        allow_entry_points=loaded.settings.plugins.allow_entry_points,
        user_dir=loaded.paths.user_plugins_dir,
        project_dir=loaded.paths.project_dir / "plugins",
        disabled=loaded.settings.plugins.disabled,
    )
    manager.load(registry=tools_registry, settings=loaded.settings)
    return runtime, manager, tools_registry


@app.command("list")
def list_plugins(ctx: typer.Context) -> None:
    """Show discovered plugins and what they contributed."""

    runtime, manager, registry = _manager(ctx)
    records = manager.summary()
    if runtime.is_machine:
        runtime.emit_json({"plugins": records, "tools_registered": len(registry)})
        return
    if not records:
        output.print("[ua.muted]no plugins found[/ua.muted]")
        output.print(f"[ua.muted]entry point group: {ENTRY_POINT_GROUP}; user: ~/.config/unknown-agent/plugins; project: .unknown/plugins[/ua.muted]")
        return
    rows = [
        (
            record["name"],
            record["version"],
            record["source"],
            "error" if record["error"] else "loaded",
            f"{len(record['tools'])} tool(s), {len(record['providers'])} provider(s)",
        )
        for record in records
    ]
    output.print(data_table(["plugin", "version", "source", "status", "contributions"], rows, title=f"plugins ({len(rows)})"))
    for record in records:
        if record["error"]:
            output.print(f"[ua.err]{record['name']}[/ua.err]: {record['error']}")


@app.command("new")
def new_plugin(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Plugin name."),
    directory: str = typer.Option("project", "--scope", help="project | user | <path>."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Scaffold a working plugin with a tool, a command and a provider hook.

    Examples:
      ua plugins new my-checks
      ua plugins new db-inspector --scope user
    """
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    if directory == "project":
        target_dir = loaded.paths.project_plugins_dir
    elif directory == "user":
        target_dir = loaded.paths.user_plugins_dir
    else:
        target_dir = Path(directory)
    slug = name.strip().replace(" ", "-").lower()
    target = target_dir / f"{slug.replace('-', '_')}.py"
    if target.exists() and not force:
        raise UsageError(f"{target} already exists.", hint="Pass --force to overwrite.")
    path = write_plugin_skeleton(target, name)
    output.print(f"[ua.ok]created[/ua.ok] {path}")
    output.print("[ua.muted]it is picked up automatically on the next run - check with `ua plugins list`[/ua.muted]")


@app.command("info")
def info(ctx: typer.Context, name: str = typer.Argument(..., help="Plugin name.")) -> None:
    """Detail for one plugin."""

    runtime, manager, _registry = _manager(ctx)
    record = next((item for item in manager.summary() if item["name"] == name), None)
    if record is None:
        raise UsageError(f"No plugin named {name!r}.", hint="Run `ua plugins list`.")
    if runtime.is_machine:
        runtime.emit_json(record)
        return
    output.print(kv_table(sorted(record.items()), title=f"plugin {name}"))


@app.command("disable")
def disable(ctx: typer.Context, name: str = typer.Argument(..., help="Plugin name to disable.")) -> None:
    """Disable a plugin by adding it to [plugins] disabled in the user config."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    target = loaded.paths.user_config_file
    import tomllib

    import tomli_w

    payload = tomllib.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    plugins_section = payload.setdefault("plugins", {})
    disabled = plugins_section.setdefault("disabled", [])
    if name not in disabled:
        disabled.append(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tomli_w.dumps(payload), encoding="utf-8")
    output.print(f"[ua.ok]disabled[/ua.ok] plugin {name!r} in {target}")


@app.command("enable")
def enable(ctx: typer.Context, name: str = typer.Argument(..., help="Plugin name to enable.")) -> None:
    """Re-enable a previously disabled plugin."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    target = loaded.paths.user_config_file
    import tomllib

    import tomli_w

    if not target.is_file():
        raise UsageError("No user config file to edit.", hint="Run `ua config init` first.")
    payload = tomllib.loads(target.read_text(encoding="utf-8"))
    disabled = payload.get("plugins", {}).get("disabled", [])
    if name in disabled:
        disabled.remove(name)
    target.write_text(tomli_w.dumps(payload), encoding="utf-8")
    output.print(f"[ua.ok]enabled[/ua.ok] plugin {name!r}")


@app.command("paths")
def paths(ctx: typer.Context) -> None:
    """Where plugins are discovered from."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    payload = {
        "entry_point_group": ENTRY_POINT_GROUP,
        "user_dir": str(loaded.paths.user_plugins_dir),
        "project_dir": str(loaded.paths.project_plugins_dir),
    }
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(kv_table(sorted(payload.items()), title="plugin paths"))


__all__ = ["app"]
