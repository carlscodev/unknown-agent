"""``ua mcp`` — the Model Context Protocol management surface.

MCP is the product's expansion system, so this group is a real management
experience, not a config-file wrapper:

* ``catalog``          the curated ecosystem, ready to add in one command
* ``add`` / ``remove`` write validated server entries (user or project scope)
* ``list`` / ``show``  what is configured, what it offers, what is excluded
* ``test``             connect + initialize + ping, with latency
* ``enable``/``disable``/``trust``  the levers, one word each
* ``tools``            every MCP tool the agent would see right now, and why
                       the ones it will not see were excluded

Everything honours ``--json``.  Connection work is fail-soft: a dead server is
a red line in a table, never a traceback.
"""

from __future__ import annotations

import asyncio
import functools
import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import tomli_w
import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.config.models import McpServerConfig, Settings
from unknown_agent.errors import ConfigError, ExitCode, UnknownAgentError, UsageError
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table, summary_panel

app = typer.Typer(
    name="mcp",
    help="Model Context Protocol servers: add, inspect, test, trust, expose.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

JSON_FLAG = typer.Option(False, "--json", help="Machine-readable JSON on stdout.")
PROJECT_FLAG = typer.Option(False, "--project", help="Write to <repo>/.unknown/config.toml instead of the user config.")

TRUST_LEVELS = ("full", "trusted", "sandboxed", "blocked")

F = TypeVar("F", bound=Callable[..., None])


def _guarded(func: F) -> F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            func(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except UnknownAgentError as exc:
            from unknown_agent.ui.errors import render_error

            raise typer.Exit(render_error(exc)) from None

    return wrapper  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if runtime is None:  # pragma: no cover
        raise UsageError("No runtime on the Typer context.")
    return runtime


def _machine(runtime: Runtime, json_flag: bool) -> bool:
    if json_flag and not runtime.is_machine:
        output.mode = "json"
    return runtime.is_machine or json_flag


def _manager(runtime: Runtime) -> Any:
    from unknown_agent.mcp.manager import McpManager

    return McpManager(runtime.load().settings)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _server_or_die(settings: Settings, name: str) -> McpServerConfig:
    config = settings.mcp.servers.get(name)
    if config is None:
        known = ", ".join(sorted(settings.mcp.servers)) or "none yet — `ua mcp catalog` shows what you can add"
        raise UsageError(f"No MCP server named {name!r}.", hint=f"Configured servers: {known}.")
    return config


def _write_server(runtime: Runtime, name: str, data: dict[str, Any] | None, *, project: bool) -> Path:
    """Persist ``[mcp.servers.<name>]`` (or delete it when ``data`` is None)."""

    loaded = runtime.load()
    target = loaded.paths.project_config_file if project else loaded.paths.user_config_file
    existing: dict[str, Any] = {}
    if target.is_file():
        existing = tomllib.loads(target.read_text(encoding="utf-8"))
    servers = existing.setdefault("mcp", {}).setdefault("servers", {})
    if data is None:
        servers.pop(name, None)
        if not servers:
            existing["mcp"].pop("servers", None)
        if not existing["mcp"]:
            existing.pop("mcp", None)
    else:
        servers[name] = data
    # Validate the whole file before writing: a bad server entry must never
    # brick every subsequent `ua` invocation.
    merged = loaded.settings.model_dump(mode="python", exclude_none=True)
    merged.setdefault("mcp", {}).setdefault("servers", {})
    if data is None:
        merged["mcp"]["servers"].pop(name, None)
    else:
        merged["mcp"]["servers"][name] = data
    try:
        Settings.model_validate(merged)
    except Exception as exc:  # noqa: BLE001 - pydantic message is the feedback
        raise ConfigError(f"Refusing to write invalid MCP configuration: {exc}") from None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tomli_w.dumps(existing), encoding="utf-8")
    runtime._loaded = None  # settings changed on disk; force the next load() to see it
    return target


def _patch_server(runtime: Runtime, name: str, patch: dict[str, Any]) -> Path:
    """Update fields of an existing server in whichever file defines it."""

    loaded = runtime.load()
    _server_or_die(loaded.settings, name)
    for candidate, is_project in ((loaded.paths.project_config_file, True), (loaded.paths.user_config_file, False)):
        if candidate.is_file():
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            entry = data.get("mcp", {}).get("servers", {}).get(name)
            if entry is not None:
                entry.update(patch)
                return _write_server(runtime, name, entry, project=is_project)
    # Configured through defaults/env only: materialise it in the user file.
    entry = loaded.settings.mcp.servers[name].model_dump(mode="python", exclude_none=True)
    entry.update(patch)
    return _write_server(runtime, name, entry, project=False)


def _trust_style(trust: str) -> str:
    return {"full": "ua.ok", "trusted": "ua.accent", "sandboxed": "ua.warn", "blocked": "ua.err"}.get(trust, "ua.dim")


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
@app.command("catalog")
@_guarded
def catalog_cmd(ctx: typer.Context, json_output: bool = JSON_FLAG) -> None:
    """The curated MCP ecosystem: servers you can add in one command.

    Examples:
      ua mcp catalog
      ua mcp add git            # then: connect the reference git server
    """

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    from unknown_agent.mcp.catalog import catalog_payload

    entries = catalog_payload()
    if machine:
        runtime.emit_json({"catalog": entries})
        return
    rows = [
        (
            str(entry["key"]) + (" *" if entry["recommended"] else ""),
            str(entry["label"]),
            str(entry["runtime"]),
            str(entry["default_trust"]),
            str(entry["description"])[:64],
        )
        for entry in entries
    ]
    output.print(data_table(["key", "label", "runtime", "trust", "what it does"], rows, title="MCP catalog"))
    output.print("[ua.dim]* recommended.  Add one with `ua mcp add <key>`; anything else with `ua mcp add <name> --command ...` or `--url ...`.[/ua.dim]")


# --------------------------------------------------------------------------- #
# add / remove
# --------------------------------------------------------------------------- #
@app.command("add")
@_guarded
def add_server(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Server name (also a catalog key, e.g. git, fetch, time)."),
    command: str | None = typer.Option(None, "--command", help="stdio: executable to launch (uvx, npx, python...)."),
    arg: list[str] = typer.Option([], "--arg", help="stdio: one launch argument (repeatable)."),
    url: str | None = typer.Option(None, "--url", help="Remote server URL (streamable HTTP)."),
    sse: bool = typer.Option(False, "--sse", help="Use legacy SSE transport for --url servers."),
    env: list[str] = typer.Option([], "--env", help="KEY=VALUE for stdio servers (value may be env:VAR / store:key)."),
    header: list[str] = typer.Option([], "--header", help="Header for remote servers, 'Name: value' (value may be a secret ref)."),
    trust: str | None = typer.Option(None, "--trust", help="full | trusted | sandboxed | blocked."),
    group: str = typer.Option("", "--group", help="Grouping label (core, web, team...)."),
    description: str = typer.Option("", "--description", help="Shown in `ua mcp list`."),
    project: bool = PROJECT_FLAG,
    test: bool = typer.Option(True, "--test/--no-test", help="Test the connection after writing."),
    json_output: bool = JSON_FLAG,
) -> None:
    """Add an MCP server: from the catalog by key, or explicitly.

    Examples:
      ua mcp add git                                     # catalog, one word
      ua mcp add fetch --trust sandboxed
      ua mcp add my-tools --command uvx --arg my-mcp-server
      ua mcp add corp --url https://mcp.corp.example/api --header 'Authorization: Bearer env:CORP_TOKEN'
    """

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    loaded = runtime.load()
    if name in loaded.settings.mcp.servers:
        raise UsageError(f"An MCP server named {name!r} already exists.", hint="Remove it first with `ua mcp remove`, or pick another name.")
    if trust is not None and trust not in TRUST_LEVELS:
        raise UsageError(f"Unknown trust level {trust!r}.", hint=f"Levels: {', '.join(TRUST_LEVELS)}.")

    entry_data: dict[str, Any]
    if command is None and url is None:
        from unknown_agent.mcp.catalog import catalog_entry, config_from_entry

        entry = catalog_entry(name)
        if entry is None:
            raise UsageError(
                f"{name!r} is not in the catalog and no --command/--url was given.",
                hint="`ua mcp catalog` lists known keys; otherwise pass --command (stdio) or --url (remote).",
            )
        root = str(loaded.project_root or Path.cwd()) if entry.needs_project_root else None
        config = config_from_entry(entry, project_root=root)
        if trust is not None:
            config.trust = trust  # type: ignore[assignment]
        if group:
            config.group = group
        entry_data = config.model_dump(mode="python", exclude_none=True)
    else:
        if command is not None and url is not None:
            raise UsageError("Give either --command (stdio) or --url (remote), not both.")
        entry_data = {
            "trust": trust or loaded.settings.mcp.default_trust,
            "group": group,
            "description": description,
            "origin": "manual",
        }
        if command is not None:
            entry_data.update({"transport": "stdio", "command": command, "args": list(arg)})
            env_map: dict[str, str] = {}
            for item in env:
                key, sep, value = item.partition("=")
                if not sep:
                    raise UsageError(f"--env needs KEY=VALUE, got {item!r}.")
                env_map[key.strip()] = value
            if env_map:
                entry_data["env"] = env_map
        else:
            entry_data.update({"transport": "sse" if sse else "http", "url": url})
            header_map: dict[str, str] = {}
            for item in header:
                key, sep, value = item.partition(":")
                if not sep:
                    raise UsageError(f"--header needs 'Name: value', got {item!r}.")
                header_map[key.strip()] = value.strip()
            if header_map:
                entry_data["headers"] = header_map

    entry_data = {key: value for key, value in entry_data.items() if value not in ("", [], {})}
    target = _write_server(runtime, name, entry_data, project=project)

    health = None
    if test:
        manager = _manager(runtime)

        async def probe() -> Any:
            try:
                return await manager.test(name)
            finally:
                await manager.close()

        health = _run(probe())

    if machine:
        payload: dict[str, Any] = {"added": name, "file": str(target), "config": entry_data}
        if health is not None:
            payload["health"] = health.to_dict()
        runtime.emit_json(payload)
        if health is not None and not health.ok:
            raise typer.Exit(ExitCode.CONFIG)
        return

    output.print(f"[ua.ok]added[/ua.ok] mcp server [ua.accent]{name}[/ua.accent] → {target}")
    if health is not None:
        if health.ok:
            output.print(f"[ua.ok]✓[/ua.ok] connection test passed ({health.latency_ms:.0f} ms)")
        else:
            output.print(f"[ua.err]✗[/ua.err] connection test failed: {health.error}")
            output.print("[ua.dim]the entry was written anyway — fix the server and re-check with `ua mcp test`[/ua.dim]")
            raise typer.Exit(ExitCode.CONFIG)
    output.print(f"[ua.dim]inspect with `ua mcp show {name}`; tools appear to the agent as mcp.{name}.<tool>[/ua.dim]")


@app.command("remove")
@_guarded
def remove_server(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Configured server name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    json_output: bool = JSON_FLAG,
) -> None:
    """Remove a configured MCP server (from whichever config file defines it)."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    loaded = runtime.load()
    _server_or_die(loaded.settings, name)
    if not (yes or machine):
        confirmed = typer.confirm(f"Remove MCP server '{name}'?", default=False)
        if not confirmed:
            raise typer.Exit(ExitCode.OK)
    removed_from: list[str] = []
    for candidate, is_project in ((loaded.paths.project_config_file, True), (loaded.paths.user_config_file, False)):
        if candidate.is_file():
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            if name in data.get("mcp", {}).get("servers", {}):
                _write_server(runtime, name, None, project=is_project)
                removed_from.append(str(candidate))
    if machine:
        runtime.emit_json({"removed": name, "files": removed_from})
        return
    if removed_from:
        output.print(f"[ua.ok]removed[/ua.ok] {name} from {', '.join(removed_from)}")
    else:
        output.print(f"[ua.warn]{name} was configured outside the editable config files (env or defaults); nothing to delete.[/ua.warn]")


# --------------------------------------------------------------------------- #
# list / show / test
# --------------------------------------------------------------------------- #
@app.command("list")
@_guarded
def list_servers(
    ctx: typer.Context,
    group: str = typer.Option("", "--group", help="Only servers in this group."),
    json_output: bool = JSON_FLAG,
) -> None:
    """All configured MCP servers, their transports and trust levels."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    manager = _manager(runtime)
    described = manager.describe()
    servers = [item for item in described["servers"] if not group or item["group"] == group]
    if machine:
        runtime.emit_json({"enabled": described["enabled"], "servers": servers})
        return
    if not described["enabled"]:
        output.print("[ua.warn][mcp] is disabled[/ua.warn] — enable with `ua config set mcp.enabled true`")
    if not servers:
        output.print("[ua.muted]no MCP servers configured[/ua.muted]")
        output.print("[ua.muted]browse the ecosystem with `ua mcp catalog`, then `ua mcp add <key>`[/ua.muted]")
        return
    rows = [
        (
            item["name"],
            item["transport"],
            "on" if item["enabled"] else "off",
            item["trust"],
            item["group"] or "-",
            item["target"][:56],
        )
        for item in servers
    ]
    output.print(data_table(["server", "transport", "enabled", "trust", "group", "target"], rows, title=f"mcp servers ({len(rows)})"))
    for item in servers:
        if item["error"]:
            output.print(f"[ua.err]{item['name']}[/ua.err]: {item['error']}")


@app.command("show")
@_guarded
def show_server(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Configured server name."),
    connect: bool = typer.Option(True, "--connect/--no-connect", help="Connect and list capabilities (default) or show config only."),
    json_output: bool = JSON_FLAG,
) -> None:
    """Full capability inspection: tools, resources, prompts, exclusions.

    Examples:
      ua mcp show git
      ua mcp show corp --no-connect --json
    """

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    loaded = runtime.load()
    _server_or_die(loaded.settings, name)
    manager = _manager(runtime)

    async def fetch() -> Any:
        try:
            return await manager.snapshot(name, connect=connect)
        finally:
            await manager.close()

    snap = _run(fetch())
    if machine:
        runtime.emit_json(snap.to_dict())
        return

    rows = [
        ("transport", snap.transport),
        ("target", snap.target),
        ("enabled", "yes" if snap.enabled else "no"),
        ("trust", snap.trust),
        ("group", snap.group or "-"),
        ("connected", "yes" if snap.connected else "no"),
    ]
    caps = snap.capabilities
    if caps:
        rows += [
            ("server", f"{caps.server_name} {caps.server_version}".strip()),
            ("protocol", caps.protocol_version or "-"),
            ("capabilities", ", ".join(k for k, v in (("tools", caps.tools), ("resources", caps.resources), ("prompts", caps.prompts), ("logging", caps.logging)) if v) or "none"),
        ]
    output.print(kv_table(rows, title=f"mcp server: {name}"))
    if snap.error:
        output.print(f"[ua.err]{snap.error}[/ua.err]")
    if caps and caps.instructions:
        output.print(summary_panel("server instructions", [(name, caps.instructions.strip()[:800])]))
    if snap.tools:
        output.print(
            data_table(
                ["tool", "exposed as", "hints", "description"],
                [
                    (
                        tool.name,
                        f"mcp.{name}.{tool.name}",
                        ("read-only" if tool.read_only_hint else ("destructive" if tool.destructive_hint else "-")),
                        (tool.description or "")[:48],
                    )
                    for tool in snap.tools
                ],
                title=f"tools ({len(snap.tools)})",
            )
        )
    if snap.excluded_tools:
        for tool_name, reason in snap.excluded_tools.items():
            output.print(f"[ua.dim]excluded[/ua.dim] {tool_name}: {reason}")
    if snap.resources:
        output.print(data_table(["resource", "type", "name"], [(item.uri[:60], item.mime_type or "-", item.name) for item in snap.resources[:20]], title=f"resources ({len(snap.resources)})"))
    if snap.prompts:
        output.print(data_table(["prompt", "args", "description"], [(item.name, str(len(item.arguments)), (item.description or "")[:48]) for item in snap.prompts], title=f"prompts ({len(snap.prompts)})"))


@app.command("test")
@_guarded
def test_server(
    ctx: typer.Context,
    name: str = typer.Argument("", help="Server to test; empty tests every enabled server."),
    json_output: bool = JSON_FLAG,
) -> None:
    """Connect, initialize and ping.  Exit 4 when any tested server fails.

    Examples:
      ua mcp test git
      ua mcp test          # the whole fleet
    """

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    loaded = runtime.load()
    if name:
        _server_or_die(loaded.settings, name)
        names = [name]
    else:
        names = sorted(loaded.settings.mcp.servers)
        if not names:
            raise UsageError("No MCP servers configured.", hint="`ua mcp catalog` shows what you can add.")
    manager = _manager(runtime)

    async def run_all() -> dict[str, Any]:
        results: dict[str, Any] = {}
        try:
            for server in names:
                results[server] = await manager.test(server)
        finally:
            await manager.close()
        return results

    results = _run(run_all())
    failed = [server for server, health in results.items() if not health.ok]
    if machine:
        runtime.emit_json({"results": {server: health.to_dict() for server, health in results.items()}, "ok": not failed})
        raise typer.Exit(ExitCode.CONFIG if failed else ExitCode.OK)
    for server, health in results.items():
        if health.ok:
            output.print(f"[ua.ok]✓[/ua.ok] {server}  ({health.latency_ms:.0f} ms)")
        else:
            output.print(f"[ua.err]✗[/ua.err] {server}: {health.error}")
    raise typer.Exit(ExitCode.CONFIG if failed else ExitCode.OK)


# --------------------------------------------------------------------------- #
# levers: enable / disable / trust
# --------------------------------------------------------------------------- #
@app.command("enable")
@_guarded
def enable_server(ctx: typer.Context, name: str = typer.Argument(...), json_output: bool = JSON_FLAG) -> None:
    """Enable a configured server."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    _server_or_die(runtime.load().settings, name)
    target = _patch_server(runtime, name, {"enabled": True})
    if machine:
        runtime.emit_json({"server": name, "enabled": True, "file": str(target)})
        return
    output.print(f"[ua.ok]enabled[/ua.ok] {name}")


@app.command("disable")
@_guarded
def disable_server(ctx: typer.Context, name: str = typer.Argument(...), json_output: bool = JSON_FLAG) -> None:
    """Disable a server without losing its configuration."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    _server_or_die(runtime.load().settings, name)
    target = _patch_server(runtime, name, {"enabled": False})
    if machine:
        runtime.emit_json({"server": name, "enabled": False, "file": str(target)})
        return
    output.print(f"[ua.warn]disabled[/ua.warn] {name} — its tools are withheld from the agent")


@app.command("trust")
@_guarded
def trust_server(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    level: str = typer.Argument("", help="full | trusted | sandboxed | blocked. Empty shows the current level."),
    json_output: bool = JSON_FLAG,
) -> None:
    """Set (or show) a server's trust level.

    full      tools run like builtins (policy still applies)
    trusted   read tools free; mutating tools confirmed        (default)
    sandboxed every call confirmed; output labelled untrusted
    blocked   inspectable, but no tool reaches the agent
    """

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    config = _server_or_die(runtime.load().settings, name)
    if not level:
        if machine:
            runtime.emit_json({"server": name, "trust": config.trust})
            return
        output.print(f"{name}: [{_trust_style(config.trust)}]{config.trust}[/{_trust_style(config.trust)}]")
        return
    if level not in TRUST_LEVELS:
        raise UsageError(f"Unknown trust level {level!r}.", hint=f"Levels: {', '.join(TRUST_LEVELS)}.")
    target = _patch_server(runtime, name, {"trust": level})
    if machine:
        runtime.emit_json({"server": name, "trust": level, "file": str(target)})
        return
    output.print(f"[ua.ok]trust[/ua.ok] {name} → [{_trust_style(level)}]{level}[/{_trust_style(level)}]")


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@app.command("tools")
@_guarded
def list_mcp_tools(ctx: typer.Context, json_output: bool = JSON_FLAG) -> None:
    """Every MCP tool the agent would see in the next run, with risk and origin."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    manager = _manager(runtime)
    from unknown_agent.mcp.bridge import build_proxies

    async def fetch() -> Any:
        try:
            return await manager.agent_tools()
        finally:
            await manager.close()

    selected, warnings = _run(fetch())
    proxies = build_proxies(selected)
    if machine:
        runtime.emit_json({"tools": [proxy.spec() for proxy in proxies], "warnings": warnings})
        return
    if not proxies:
        output.print("[ua.muted]no MCP tools are currently exposed[/ua.muted]")
        for warning in warnings:
            output.print(f"[ua.warn]{warning}[/ua.warn]")
        return
    rows = [(proxy.name, proxy.server, proxy.risk.value, (proxy.info.description or "")[:52]) for proxy in proxies]
    output.print(data_table(["tool", "server", "risk", "description"], rows, title=f"mcp tools ({len(rows)})"))
    for warning in warnings:
        output.print(f"[ua.warn]{warning}[/ua.warn]")


__all__ = ["app"]
