"""``ua config`` - inspect, validate and edit configuration.

``--explain`` is the differentiator: it prints which layer produced every value
(default, user file, project file, environment variable or CLI flag), so nobody
has to guess why a setting is what it is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.config.loader import DEFAULT_CONFIG_TEMPLATE, dump_settings, write_default_config
from unknown_agent.errors import ConfigError, ExitCode, UsageError
from unknown_agent.ui.console import output
from unknown_agent.ui.prompts import confirm_destructive

app = typer.Typer(name="config", help="Inspect, validate and edit Unknown Agent configuration.", no_args_is_help=False, invoke_without_command=True)


def _flatten(payload: dict, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in payload.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


@app.callback(invoke_without_command=True)
def show(
    ctx: typer.Context,
    explain: bool = typer.Option(False, "--explain", help="Show which layer produced each value."),
    section: str | None = typer.Option(None, "--section", help="Only show one section (agent, policy, browser, ...)."),
    effective: bool = typer.Option(False, "--effective", help="Print the fully resolved configuration as TOML."),
    secrets: bool = typer.Option(False, "--show-secrets", help="Reveal secret references (never values)."),
) -> None:
    """Show the effective configuration.

    Examples:
      ua config show
      ua config show --explain --section agent
      ua config show --effective > my-config.toml
    """
    if ctx.invoked_subcommand is not None:
        # `ua config` shows the configuration; `ua config <subcommand>` must not also
        # dump the whole file first.
        return

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    settings = loaded.settings

    if effective:
        import tomli_w

        output.raw(tomli_w.dumps(settings.model_dump(mode="python", exclude_none=True)))
        return

    payload = settings.model_dump(mode="python", exclude_none=True)
    if section:
        if section not in payload:
            raise UsageError(f"Unknown section {section!r}.", hint=f"Valid sections: {', '.join(sorted(payload))}")
        payload = {section: payload[section]}

    if runtime.is_machine:
        runtime.emit_json(payload)
        return

    flat = _flatten(payload)
    for dotted, value in sorted(flat.items()):
        line = f"[ua.kv.key]{dotted}[/ua.kv.key] = [ua.kv.value]{json.dumps(value, default=str)}[/ua.kv.value]"
        if explain:
            source = loaded.explain(dotted) if not dotted.startswith("providers.") else loaded.explain(dotted)
            line += f"   [ua.muted]({source})[/ua.muted]"
        output.print(line)
    if not explain:
        output.print("\n[ua.muted]tip: add --explain to see which layer produced each value[/ua.muted]")


@app.command("show", hidden=False)
def show_cmd(
    ctx: typer.Context,
    explain: bool = typer.Option(False, "--explain", help="Show which layer produced each value."),
    section: str | None = typer.Option(None, "--section", help="Only show one section (agent, policy, browser, ...)."),
    effective: bool = typer.Option(False, "--effective", help="Print the fully resolved configuration as TOML."),
    secrets: bool = typer.Option(False, "--show-secrets", help="Reveal secret references (never values)."),
) -> None:
    """Show the effective configuration (explicit form of `ua config`).

    Examples:
      ua config show --explain --section agent
      ua config show --effective > my-config.toml
    """

    show(ctx, explain=explain, section=section, effective=effective, secrets=secrets)


@app.command("get")
def get_value(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Dotted path, e.g. agent.max_steps."),
) -> None:
    """Print a single value (script-friendly)."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    node: object = loaded.settings.model_dump(mode="python", exclude_none=True)
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise UsageError(f"Unknown key {key!r}.")
        node = node[part]
    if isinstance(node, (dict, list)):
        output.raw(json.dumps(node, indent=2, default=str))
    else:
        output.raw(str(node))


@app.command("set")
def set_value(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Dotted path, e.g. agent.max_steps."),
    value: str = typer.Argument(..., help="New value (JSON or plain string)."),
    project: bool = typer.Option(False, "--project", help="Write to <repo>/.unknown/config.toml instead of the user config."),
) -> None:
    """Set a configuration value, validating it against the schema.

    Examples:
      ua config set agent.max_steps 80
      ua config set providers.default.model '"gpt-4.1"' --project
    """

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value

    from unknown_agent.config.models import Settings

    payload = loaded.settings.model_dump(mode="python", exclude_none=True)
    node = payload
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise UsageError(f"Cannot set {key!r}: {part} is a value, not a table.")
    node[parts[-1]] = parsed
    try:
        Settings.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - pydantic error is user feedback
        raise ConfigError(f"Refusing to write invalid configuration: {exc}", hint="Check the type and allowed values for this key.") from exc

    target = loaded.paths.project_config_file if project else loaded.paths.user_config_file
    import tomllib

    import tomli_w

    existing: dict = {}
    if target.is_file():
        existing = tomllib.loads(target.read_text(encoding="utf-8"))
    cursor = existing
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = parsed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tomli_w.dumps(existing), encoding="utf-8")
    output.print(f"[ua.ok]set[/ua.ok] {key} = {json.dumps(parsed)} in {target}")


@app.command("init")
def init_config(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
    project: bool = typer.Option(False, "--project", help="Create the project config instead of the user config."),
    print_only: bool = typer.Option(False, "--print", help="Print the template instead of writing it."),
) -> None:
    """Create a documented starter configuration file."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    if print_only:
        output.raw(DEFAULT_CONFIG_TEMPLATE)
        return
    target = loaded.paths.project_config_file if project else loaded.paths.user_config_file
    path = write_default_config(target, force=force)
    output.print(f"[ua.ok]created[/ua.ok] {path}")
    output.print("[ua.muted]next: set your API key reference, then run `ua doctor`[/ua.muted]")


@app.command("validate")
def validate(ctx: typer.Context) -> None:
    """Validate the configuration and every reference it contains."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    problems = list(loaded.warnings)
    if problems:
        if runtime.is_machine:
            runtime.emit_json({"valid": False, "problems": problems})
        else:
            for problem in problems:
                output.print(f"[ua.warn]![/ua.warn] {problem}")
        raise typer.Exit(ExitCode.CONFIG)
    if runtime.is_machine:
        runtime.emit_json({"valid": True, "problems": []})
    else:
        output.print("[ua.ok]configuration is valid[/ua.ok]")


@app.command("path")
def path_cmd(ctx: typer.Context) -> None:
    """Print every path Unknown Agent uses (config, state, cache, project)."""

    runtime: Runtime = ctx.obj
    if runtime.is_machine:
        runtime.emit_json(runtime.paths.as_dict())
        return
    from unknown_agent.ui.renderers import kv_table

    output.print(kv_table(sorted(runtime.paths.as_dict().items())))


@app.command("edit")
def edit(ctx: typer.Context, project: bool = typer.Option(False, "--project", help="Edit the project config.")) -> None:
    """Open the configuration file in ``$EDITOR``."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    target = loaded.paths.project_config_file if project else loaded.paths.user_config_file
    if not target.is_file():
        write_default_config(target)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise UsageError("No editor configured.", hint="Set $EDITOR (or $VISUAL) and retry. File: " + str(target))
    import subprocess

    subprocess.run([*editor.split(), str(target)], check=False)  # noqa: S603 - user's own editor


@app.command("rm-secret")
def rm_secret(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Provider profile name, e.g. default."),
) -> None:
    """Remove the API key reference for a provider profile (destructive)."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    target = loaded.paths.user_config_file
    if not target.is_file():
        raise ConfigError(f"No user config at {target}.")
    if not confirm_destructive(runtime.prompter, f"remove the api_key reference for provider {name!r} in {target}"):
        output.print("[ua.muted]cancelled[/ua.muted]")
        raise typer.Exit(ExitCode.OK)
    import tomllib

    import tomli_w

    payload = tomllib.loads(target.read_text(encoding="utf-8"))
    profile = payload.get("providers", {}).get(name)
    if not isinstance(profile, dict) or "api_key" not in profile:
        raise ConfigError(f"Provider {name!r} has no api_key entry to remove.")
    profile.pop("api_key")
    target.write_text(tomli_w.dumps(payload), encoding="utf-8")
    output.print(f"[ua.ok]removed[/ua.ok] providers.{name}.api_key from {target}")


@app.command("dump")
def dump_effective(ctx: typer.Context, out: Path = typer.Option(Path("effective-config.toml"), "--out", help="Where to write the snapshot.")) -> None:
    """Write the fully resolved configuration to a file (for audit/reproducibility)."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    dump_settings(loaded.settings, out)
    output.print(f"[ua.ok]wrote[/ua.ok] {out}")


__all__ = ["app"]
