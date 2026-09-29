"""``ua plugins`` - discover, scaffold and audit extensions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.extensions.loader import ENTRY_POINT_GROUP, PluginManager, write_plugin_skeleton
from unknown_agent.extensions.manifest import (
    CapabilityDeclaration,
    CapabilityKind,
    Permission,
    ToolRisk,
    TrustStore,
    build_manifest,
    default_manifest_path,
    generate_keypair,
    sign_manifest,
    verify_manifest,
)
from unknown_agent.tools.factory import build_default_registry
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table

app = typer.Typer(name="plugins", help="Discover, scaffold and inspect plugins.", no_args_is_help=True)


def _manager(ctx: typer.Context, *, registry: Any = None) -> tuple[Runtime, PluginManager, Any]:
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    tools_registry = registry or build_default_registry(loaded.settings)
    manager = PluginManager(
        allow_entry_points=loaded.settings.plugins.allow_entry_points,
        user_dir=loaded.paths.user_plugins_dir,
        project_dir=loaded.paths.project_dir / "plugins",
        disabled=loaded.settings.plugins.disabled,
        require_signed=loaded.settings.plugins.require_signed,
        trust_store_path=loaded.paths.config_dir / "trusted_publishers.json",
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
    output.print(
        "[ua.muted]sign it before loading: `ua plugins keygen`, then "
        f"`ua plugins sign {path}`[/ua.muted]"
    )


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


@app.command("keygen")
def keygen(
    ctx: typer.Context,
    name: str = typer.Option("default", "--name", help="Signing key name."),
) -> None:
    """Generate a local Ed25519 publisher keypair without overwriting keys."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    directory = loaded.paths.config_dir / "signing"
    private_path = directory / f"{name}.key"
    public_path = directory / f"{name}.pub"
    key_id, public_key = generate_keypair(private_path, public_path)
    payload = {
        "key_id": key_id,
        "private_key": str(private_path),
        "public_key": str(public_path),
        "public_key_base64": public_key,
    }
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(kv_table(sorted(payload.items()), title="publisher key generated"))


@app.command("trust")
def trust_publisher(
    ctx: typer.Context,
    public_key_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    publisher: str = typer.Option(..., "--publisher", help="Publisher identity bound to this key."),
) -> None:
    """Explicitly trust a publisher public key for extension loading."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    store = TrustStore(loaded.paths.config_dir / "trusted_publishers.json")
    entry = store.trust(public_key_file.read_text(encoding="utf-8").strip(), publisher=publisher)
    payload = entry.model_dump(mode="json")
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(kv_table(sorted(payload.items()), title="trusted publisher"))


@app.command("sign")
def sign_plugin(
    ctx: typer.Context,
    plugin_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    name: str = typer.Option("", "--name", help="Manifest name; defaults to the file stem."),
    version: str = typer.Option("0.1.0", "--version"),
    publisher: str = typer.Option("local", "--publisher"),
    key_name: str = typer.Option("default", "--key"),
    permission: list[str] = typer.Option(
        ["tools.register", "tools.read"], "--permission", help="Repeat for every grant."
    ),
    capability: list[str] = typer.Option(
        [], "--capability", help="Repeat kind:name[:risk], e.g. tool:lint_check:read."
    ),
) -> None:
    """Create an Ed25519-signed capability manifest beside a plugin file."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    directory = loaded.paths.config_dir / "signing"
    private_path = directory / f"{key_name}.key"
    public_path = directory / f"{key_name}.pub"
    if not private_path.is_file() or not public_path.is_file():
        raise UsageError("Signing key not found.", hint="Run `ua plugins keygen` first.")
    declarations: list[CapabilityDeclaration] = []
    values = capability or [f"tool:{plugin_file.stem}_tool:read"]
    for raw in values:
        parts = raw.split(":")
        if len(parts) not in {2, 3}:
            raise UsageError(f"Invalid capability {raw!r}; expected kind:name[:risk].")
        declarations.append(
            CapabilityDeclaration(
                kind=cast(CapabilityKind, parts[0]),
                name=parts[1],
                risk=cast(ToolRisk, parts[2]) if len(parts) == 3 else None,
            )
        )
    manifest = build_manifest(
        plugin_file,
        name=name or plugin_file.stem.replace("_", "-"),
        version=version,
        entrypoint=plugin_file.name,
        publisher=publisher,
        public_key=public_path.read_text(encoding="utf-8").strip(),
        permissions=cast(list[Permission], permission),
        capabilities=declarations,
    )
    signed = sign_manifest(manifest, private_path)
    target = default_manifest_path(plugin_file)
    target.write_text(signed.to_json(), encoding="utf-8")
    payload = {"manifest": str(target), "key_id": signed.key_id, "publisher": publisher}
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(kv_table(sorted(payload.items()), title="plugin signed"))


@app.command("verify")
def verify_plugin(
    ctx: typer.Context,
    plugin_file: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    """Verify a plugin without importing or executing any of its code."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    report = verify_manifest(
        default_manifest_path(plugin_file),
        plugin_file,
        TrustStore(loaded.paths.config_dir / "trusted_publishers.json"),
        expected_entrypoint=plugin_file.name,
    )
    payload = report.model_dump(mode="json")
    if runtime.is_machine:
        runtime.emit_json(payload)
        raise typer.Exit(code=0 if report.ok else 1)
    if report.ok:
        output.print("[ua.ok]verified[/ua.ok] signature, content, runtime API and publisher trust")
    else:
        for error in report.errors:
            output.print(f"[ua.err]failed[/ua.err] {error}")
        raise typer.Exit(code=1)


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
