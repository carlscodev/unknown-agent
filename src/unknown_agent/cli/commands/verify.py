"""``ua verify`` - run the quality gates that define done."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.context.project import load_project_context
from unknown_agent.core.models import GateStatus
from unknown_agent.errors import ExitCode
from unknown_agent.observability.events import EventBus
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import gate_table, status_label
from unknown_agent.validation.engine import ValidationEngine

GATE_NAMES = ("syntax", "lint", "format", "typecheck", "tests", "build", "browser", "diff")


def verify_command(
    ctx: typer.Context,
    gate: list[str] = typer.Option(None, "--gate", help="Only run these gates (repeatable)."),
    required_only: bool = typer.Option(False, "--required-only", help="Skip advisory gates."),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop at the first failing gate."),
    url: str | None = typer.Option(None, "--url", help="App URL for the browser gate."),
    changed_only: bool = typer.Option(False, "--changed-only", help="Fast gates inspect only git-changed files."),
    save: bool = typer.Option(False, "--save", help="Persist the report to .unknown/artifacts/verification.json."),
    list_gates: bool = typer.Option(False, "--list", help="Show which gates would run and exit."),
    advisory_ok: bool = typer.Option(False, "--advisory-ok", help="Exit 0 when only advisory gates fail."),
) -> None:
    """Run the project's quality gates and report evidence.

    Exit codes: 0 every gate passed, 7 any gate failed (use --advisory-ok to ignore advisory
    failures, or --required-only to skip advisory gates entirely).

    Examples:
      ua verify
      ua verify --gate tests --gate lint --json
      ua verify --list
    """
    runtime: Runtime = ctx.obj
    runtime.start_logging()
    loaded = runtime.load()
    root = Path(loaded.project_root or loaded.paths.cwd)
    project = load_project_context(root, loaded.settings)

    engine = ValidationEngine(
        loaded.settings,
        project_root=root,
        project=project,
        events=EventBus(),
        browser=_browser_manager(loaded),
    )

    if list_gates:
        rows = []
        for gate_object in engine.gates:
            applies, reason = gate_object.applicable(_context_for(engine, loaded, project, url))
            setting = loaded.settings.validation.gate(gate_object.name)
            rows.append(
                {
                    "gate": gate_object.name,
                    "enabled": setting.enabled,
                    "required": setting.required
                    if setting.required is not None
                    else loaded.settings.validation.is_required(gate_object.name, loaded.settings.agent.definition_of_done),
                    "applies": applies,
                    "detail": reason if not applies else str(reason)[:80],
                }
            )
        if runtime.is_machine:
            runtime.emit_json({"gates": rows})
            return
        from unknown_agent.ui.renderers import data_table

        output.print(
            data_table(
                ["gate", "enabled", "required", "applies", "detail"],
                [(row["gate"], row["enabled"], row["required"], row["applies"], row["detail"]) for row in rows],
                title="gates",
            )
        )
        return

    changed = _changed_files(root) if changed_only else None
    with output.status("running gates...") as status:
        report = asyncio.run(
            engine.run(
                gates=list(gate or []) or None,
                required_only=required_only,
                base_url=url,
                changed_files=changed,
                fail_fast=fail_fast or None,
            )
        )
        if status is not None:
            status.update("gates finished")

    if save:
        engine.persist(report, Path(loaded.paths.artifacts_dir) / "verification.json")

    if runtime.is_machine:
        runtime.emit_json(report.to_dict())
    else:
        output.blank()
        output.print(gate_table(report.results))
        failures = report.failures
        if failures:
            output.blank()
            output.print("[ua.err]failing gates[/ua.err]")
            for failure in failures:
                output.print(status_label(False, fail_text=f"{failure.name}: {failure.summary}"))
                if failure.fix_hint:
                    output.print(f"    [ua.muted]hint:[/ua.muted] {failure.fix_hint}")
                if failure.output:
                    tail = "\n".join(failure.output.splitlines()[-12:])
                    output.print(f"    [ua.dim]{tail}[/ua.dim]")
        else:
            output.blank()
            output.print("[ua.ok]all gates passed[/ua.ok]")

    blocking = report.blocking_failures
    any_failure = report.failures
    failed_for_exit = blocking if advisory_ok else any_failure
    if failed_for_exit:
        raise typer.Exit(ExitCode.GATE)
    raise typer.Exit(ExitCode.OK)


def _browser_manager(loaded: Any) -> Any:
    """Build a browser manager when browser validation is usable, else ``None``.

    Keeping this lazy means `ua verify` stays fast on machines without Playwright
    and never imports it unless the browser gate is actually going to run.
    """

    settings = loaded.settings
    if not settings.browser.enabled or not settings.validation.gate("browser").enabled:
        return None
    from unknown_agent.browser.manager import BrowserSessionManager, playwright_available

    available, _message = playwright_available()
    if not available:
        return None
    return BrowserSessionManager(settings.browser, artifacts_dir=loaded.paths.artifacts_dir)


def _context_for(engine: ValidationEngine, loaded: object, project: object, url: str | None):  # pragma: no cover - structural helper
    from unknown_agent.validation.gates import GateContext

    settings = getattr(loaded, "settings")
    return GateContext(
        settings=settings,
        project_root=Path(getattr(project, "root")),
        project=project,
        browser=None,
        base_url=url or settings.browser.base_url,
    )


def _changed_files(root: Path) -> list[str]:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return []
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not files:
        try:
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            files = [line.strip() for line in untracked.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return []
    return files


__all__ = ["verify_command", "GATE_NAMES"]
