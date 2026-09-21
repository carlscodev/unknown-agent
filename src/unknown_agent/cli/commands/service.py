"""``ua service`` — optional background integration, honestly scoped.

Unknown Agent does not need a daemon.  What it offers is *scheduled
maintenance* via a user-level systemd timer, plus the verb that timer calls:

* ``list``               what units exist, why you would want them, their state
* ``install <unit>``     write + enable the user unit (no root, ever)
* ``remove <unit>``      disable + delete it
* ``status [unit]``      timer/service state and last result
* ``run-maintenance``    the actual job - also runnable by hand or by cron/
                         Task Scheduler on non-systemd machines

Non-systemd platforms are not second-class: ``run-maintenance`` is a plain
idempotent command, and ``install`` explains the cron/schtasks equivalent
instead of failing cryptically.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import ExitCode, UnknownAgentError, UsageError
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table

app = typer.Typer(
    name="service",
    help="Optional background maintenance: systemd user units, manual runs, status.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

JSON_FLAG = typer.Option(False, "--json", help="Machine-readable JSON on stdout.")

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


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if runtime is None:  # pragma: no cover
        raise UsageError("No runtime on the Typer context.")
    return runtime


def _machine(runtime: Runtime, json_flag: bool) -> bool:
    if json_flag and not runtime.is_machine:
        output.mode = "json"
    return runtime.is_machine or json_flag


def _spec(name: str):
    from unknown_agent.host.systemd import UNITS

    for unit in UNITS:
        if unit.key == name:
            return unit
    raise UsageError(f"Unknown unit {name!r}.", hint=f"Available: {', '.join(u.key for u in UNITS)} (see `ua service list`).")


# --------------------------------------------------------------------------- #
@app.command("list")
@_guarded
def list_units(ctx: typer.Context, json_flag: bool = JSON_FLAG) -> None:
    """Available units, what they do, and whether they are installed."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    from unknown_agent.host.systemd import UNITS, systemd_available, unit_status

    available, why = systemd_available()
    entries = [{"unit": spec.key, "description": spec.description, "rationale": spec.rationale, **unit_status(spec)} for spec in UNITS]
    if machine:
        runtime.emit_json({"systemd": {"available": available, "detail": why}, "units": entries})
        return
    state = "[ua.ok]available[/ua.ok]" if available else f"[ua.warn]unavailable[/ua.warn] — {why}"
    output.print(f"systemd (user manager): {state}")
    rows = [(item["unit"], item["installed"], item.get("timer", "-"), item["description"][:64]) for item in entries]
    output.print(data_table(["unit", "installed", "timer (enabled/active)", "description"], rows, title="service units"))
    output.print("[ua.muted]install with `ua service install <unit>`; every unit is optional and user-level (no root)[/ua.muted]")


@app.command("install")
@_guarded
def install(
    ctx: typer.Context,
    unit: str = typer.Argument("maintenance", help="Unit key from `ua service list`."),
    enable: bool = typer.Option(True, "--enable/--no-enable", help="Enable and start the timer now (default) or just write files."),
    json_flag: bool = JSON_FLAG,
) -> None:
    """Install a user-level systemd unit for this account (no root)."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    spec = _spec(unit)
    from unknown_agent.host.systemd import install_unit, systemd_available

    available, why = systemd_available()
    if not available:
        payload = {
            "installed": False,
            "reason": why,
            "alternatives": {
                "manual": "ua service run-maintenance",
                "cron": "@weekly ua --non-interactive --quiet service run-maintenance",
                "windows": 'schtasks /Create /SC WEEKLY /TN "Unknown Agent maintenance" /TR "ua --non-interactive --quiet service run-maintenance"',
            },
        }
        if machine:
            runtime.emit_json(payload)
            raise typer.Exit(ExitCode.FAILURE)
        output.error_panel(
            "systemd is not usable here",
            why,
            hint="The same job runs anywhere: `ua service run-maintenance` by hand, from cron (@weekly), or Windows Task Scheduler.",
        )
        raise typer.Exit(ExitCode.FAILURE)

    actions = install_unit(spec, enable_now=enable)
    if machine:
        runtime.emit_json({"installed": True, "unit": spec.key, "actions": actions})
        return
    for action in actions:
        output.print(f"  [ua.ok]✓[/ua.ok] {action}")
    output.print(f"\n[ua.muted]inspect with `ua service status {spec.key}`; remove anytime with `ua service remove {spec.key}`[/ua.muted]")


@app.command("remove")
@_guarded
def remove(
    ctx: typer.Context,
    unit: str = typer.Argument("maintenance"),
    json_flag: bool = JSON_FLAG,
) -> None:
    """Disable and delete a previously installed unit."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    spec = _spec(unit)
    from unknown_agent.host.systemd import remove_unit, unit_dir

    if not (unit_dir() / spec.service_name).exists():
        if machine:
            runtime.emit_json({"removed": False, "reason": "not installed"})
            return
        output.print(f"[ua.muted]{spec.key} is not installed - nothing to remove[/ua.muted]")
        return
    actions = remove_unit(spec)
    if machine:
        runtime.emit_json({"removed": True, "actions": actions})
        return
    for action in actions:
        output.print(f"  [ua.ok]✓[/ua.ok] {action}")


@app.command("status")
@_guarded
def status(
    ctx: typer.Context,
    unit: str = typer.Argument("maintenance"),
    json_flag: bool = JSON_FLAG,
) -> None:
    """Timer and last-run state for a unit."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    spec = _spec(unit)
    from unknown_agent.host.systemd import unit_status

    info = unit_status(spec)
    if machine:
        runtime.emit_json(info)
        return
    output.print(kv_table(sorted(info.items()), title=f"service: {spec.key}"))
    if info.get("installed") != "yes":
        output.print(f"[ua.muted]install with `ua service install {spec.key}`[/ua.muted]")


# --------------------------------------------------------------------------- #
@app.command("run-maintenance")
@_guarded
def run_maintenance(ctx: typer.Context, json_flag: bool = JSON_FLAG) -> None:
    """Run the maintenance job once: prune, rotate, cache the update check.

    Idempotent and quiet by design - safe from a timer, cron, CI, or by hand.
    """

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    settings = runtime.settings
    paths = runtime.paths
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 1. session index pruning (user scope)
    try:
        from unknown_agent.memory.sessions import SessionStore

        store = SessionStore(paths.sessions_db)
        report["sessions_pruned"] = store.prune(settings.memory.sessions_retention)
        store.close()
    except Exception as exc:  # noqa: BLE001 - each step degrades independently
        report["sessions_pruned"] = f"skipped ({exc})"

    # 2. project memory retention (only when run inside a project)
    try:
        if paths.project_dir.exists() and settings.memory.retention_days:
            from unknown_agent.memory.store import MemoryStore

            store = MemoryStore(paths.project_memory_dir / "memory.db")
            report["memories_pruned"] = store.prune(settings.memory.retention_days)
            store.close()
        else:
            report["memories_pruned"] = 0
    except Exception as exc:  # noqa: BLE001
        report["memories_pruned"] = f"skipped ({exc})"

    # 3. log rotation: cap the user log at 5 MiB, keep one predecessor
    try:
        log_file = paths.log_file
        rotated = False
        if log_file.is_file() and log_file.stat().st_size > 5 * 1024 * 1024:
            backup = log_file.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            log_file.rename(backup)
            rotated = True
        report["log_rotated"] = rotated
    except OSError as exc:
        report["log_rotated"] = f"skipped ({exc})"

    # 4. stale cache cleanup (>30 days)
    try:
        removed = 0
        cutoff = time.time() - 30 * 86400
        cache_root = paths.cache_dir
        if cache_root.is_dir():
            for item in cache_root.rglob("*"):
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
                    removed += 1
        report["cache_files_removed"] = removed
    except OSError as exc:
        report["cache_files_removed"] = f"skipped ({exc})"

    # 5. cache the update check so interactive startups never pay for it
    try:
        from unknown_agent import __version__
        from unknown_agent.cli.commands.misc import _latest_version

        latest = _latest_version()
        stamp = paths.state_dir / "update-check.json"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(
            json.dumps({"checked_at": time.time(), "installed": __version__, "latest": latest}),
            encoding="utf-8",
        )
        report["update_check"] = latest or "unreachable"
    except Exception as exc:  # noqa: BLE001
        report["update_check"] = f"skipped ({exc})"

    if machine:
        runtime.emit_json(report)
        return
    output.print(kv_table(sorted(report.items()), title="maintenance"))


__all__ = ["app"]
