"""Agent commands: ``run``, ``build``, ``fix``, ``plan``, ``ask``.

These are five views of one execution engine, each with a different contract:

| command | writes | verification | typical use                                    |
| ------- | ------ | ------------ | ---------------------------------------------- |
| run     | yes    | yes          | general task execution with a completion bar   |
| build   | yes    | yes (strict) | greenfield feature work, definition of done    |
| fix     | yes    | yes + regression guard | reproduce, diagnose, repair, prove  |
| plan    | no     | n/a          | review the approach before spending tokens     |
| ask     | no     | n/a          | read-only codebase questions                   |

All five share the same options so muscle memory transfers, and all five honour
the global flags (``--model``, ``--provider``, ``--max-steps``, ``--json``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from unknown_agent.cli.render import render_run_summary
from unknown_agent.cli.runtime import Runtime
from unknown_agent.core.models import RunStatus
from unknown_agent.core.runner import RunRequest
from unknown_agent.errors import ExitCode, UsageError
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import header, plan_tree

COMMON_OPTIONS = {
    "task": typer.Argument(None, help="What to do, in plain language. Omit to read from stdin."),
}


def _task_from_args(task: str | None) -> str:
    if task:
        return task.strip()
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            return piped
    raise UsageError(
        "No task provided.",
        hint='Pass the task as an argument: ua run "add a health endpoint with tests"',
    )


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if not isinstance(runtime, Runtime):  # pragma: no cover - defensive
        raise UsageError("CLI runtime is not initialised.")
    return runtime


def _ensure_configured(runtime: Runtime) -> None:
    """No provider yet?  Open the setup panel instead of failing with a hint.

    This is what makes the guided panel the *default* path: a brand-new user can
    type ``ua run "..."`` and be walked through provider, key and model without
    ever learning a configuration command.  Automation (``--json``, ``--non-interactive``,
    no TTY) keeps the old, explicit error so scripts never hang on a UI.
    """

    from unknown_agent.onboarding.detect import detect_setup_state
    from unknown_agent.onboarding.panel import launch_setup, panel_available

    state = detect_setup_state(runtime.load().settings)
    if state.ready:
        return
    if not panel_available(runtime):
        from unknown_agent.errors import ConfigError

        raise ConfigError(
            state.reason or "No provider is configured.",
            hint=state.hint or "Run `ua setup` in a terminal to open the guided setup panel.",
        )
    output.print(f"[ua.warn]{state.reason or 'not configured yet'} — opening the setup panel …[/ua.warn]")
    outcome = launch_setup(runtime)
    if not outcome.ok:
        raise typer.Exit(ExitCode.CONFIG)


def _execute(
    ctx: typer.Context,
    *,
    task: str,
    kind: str,
    read_only: bool,
    dry_run: bool,
    definition_of_done: str | None,
    browser: bool | None,
    url: str | None,
    gates: list[str] | None,
    resume: str | None,
    show_diff: bool,
    allow_gate_failure: bool,
    orchestrate: bool | None = None,
) -> None:
    runtime = _runtime(ctx)
    runtime.start_logging()
    _ensure_configured(runtime)
    loaded = runtime.load()
    overrides = {"read_only": True} if read_only else None
    if overrides:
        for key, value in overrides.items():
            setattr(loaded.settings.policy, key, value)
    request = RunRequest(
        task=task,
        kind=kind,
        read_only=read_only,
        dry_run=dry_run,
        yes=runtime.assume_yes,
        non_interactive=runtime.non_interactive,
        model=runtime.model,
        provider=runtime.provider,
        max_steps=runtime.max_steps,
        definition_of_done=definition_of_done or runtime.definition_of_done,
        gates=gates,
        browser_url=url,
        invoke_browser=browser,
        resume_run_id=resume,
        orchestrate=orchestrate,
    )

    if not runtime.is_machine:
        header(
            f"{kind} :: {task[:100]}",
            subtitle=f"mode={loaded.settings.agent.mode} dod={request.definition_of_done or loaded.settings.agent.definition_of_done} "
            f"model={loaded.settings.active_provider.model}",
        )

    result = runtime.execute_run(request)
    if runtime.is_machine:
        runtime.emit_json(result.to_dict())
        if result.state.status in {RunStatus.FAILED, RunStatus.BLOCKED} and not allow_gate_failure:
            raise typer.Exit(ExitCode.BLOCKED if result.state.status is RunStatus.BLOCKED else ExitCode.FAILURE)
        return

    if kind == "plan" and result.state.plan is not None:
        output.blank()
        output.print(plan_tree(result.state.plan.steps, title=f"Plan: {result.state.plan.goal[:80]}"))
        if result.state.plan.deliverables:
            output.print("\n[ua.muted]deliverables[/ua.muted]")
            for item in result.state.plan.deliverables:
                output.print(f"  • {item}")
        if result.state.plan.risks:
            output.print("\n[ua.muted]risks[/ua.muted]")
            for item in result.state.plan.risks:
                output.print(f"  ! {item}")
        output.print(f"\n[ua.muted]no files were modified. Execute with:[/ua.muted] ua run \"{result.state.task[:80]}\"")
        return

    render_run_summary(result, show_diff=show_diff)
    if result.state.status is RunStatus.INTERRUPTED:
        raise typer.Exit(ExitCode.INTERRUPTED)
    if result.state.status is RunStatus.BLOCKED and not allow_gate_failure:
        raise typer.Exit(ExitCode.BLOCKED)
    if result.state.status is RunStatus.FAILED and not allow_gate_failure:
        raise typer.Exit(ExitCode.FAILURE)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def run_command(
    ctx: typer.Context,
    task: str = COMMON_OPTIONS["task"],
    read_only: bool = typer.Option(False, "--read-only", help="Do not modify anything; investigate and report."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan and simulate writes without applying them."),
    dod: str | None = typer.Option(None, "--dod", help="strict | balanced | advisory."),
    browser: bool | None = typer.Option(None, "--browser/--no-browser", help="Force browser validation on/off."),
    url: str | None = typer.Option(None, "--url", help="App URL for browser validation."),
    gates: list[str] = typer.Option(None, "--gate", help="Only run these gates (repeatable)."),
    resume: str | None = typer.Option(None, "--resume", help="Resume a previous run id, keeping its plan."),
    diff: bool = typer.Option(False, "--diff", help="Show the working-tree diff at the end."),
    allow_failure: bool = typer.Option(False, "--allow-gate-failure", help="Exit 0 even when gates fail (report only)."),
    orchestrate: bool | None = typer.Option(
        None,
        "--orchestrate/--solo",
        # The bracket is escaped: Rich parses help strings as markup, so a bare
        # "[orchestration]" is swallowed as a style tag and the sentence reads
        # "decide from  and task complexity".
        help="Force multi-worker delegation on or off (default: decide from \[orchestration] and task complexity).",
    ),
) -> None:
    """Execute a task end-to-end: plan, act, verify, self-heal, report.

    Examples:
      ua run "add pagination to /users with tests"
      ua run "refactor auth into a service layer" --dod strict
      ua run "document the deploy process" --read-only
    """
    _execute(
        ctx,
        task=_task_from_args(task),
        kind="run",
        read_only=read_only,
        dry_run=dry_run,
        definition_of_done=dod,
        browser=browser,
        url=url,
        gates=list(gates or []) or None,
        resume=resume,
        show_diff=diff,
        allow_gate_failure=allow_failure,
        orchestrate=orchestrate,
    )


def build_command(
    ctx: typer.Context,
    task: str = COMMON_OPTIONS["task"],
    dod: str | None = typer.Option("strict", "--dod", help="Definition of done (strict by default for builds)."),
    browser: bool | None = typer.Option(None, "--browser/--no-browser", help="Force browser validation on/off."),
    url: str | None = typer.Option(None, "--url", help="App URL for browser validation."),
    gates: list[str] = typer.Option(None, "--gate", help="Only run these gates (repeatable)."),
    diff: bool = typer.Option(True, "--diff/--no-diff", help="Show the working-tree diff at the end."),
    allow_failure: bool = typer.Option(False, "--allow-gate-failure", help="Exit 0 even when gates fail."),
    orchestrate: bool | None = typer.Option(
        None,
        "--orchestrate/--solo",
        # The bracket is escaped: Rich parses help strings as markup, so a bare
        # "[orchestration]" is swallowed as a style tag and the sentence reads
        # "decide from  and task complexity".
        help="Force multi-worker delegation on or off (default: decide from \[orchestration] and task complexity).",
    ),
) -> None:
    """Build something new with a strict completion bar.

    Examples:
      ua build "a FastAPI service with /health, tests and Dockerfile"
      ua build "Next.js dashboard with auth and a settings page" --url http://localhost:3000
    """
    _execute(
        ctx,
        task=_task_from_args(task),
        kind="build",
        read_only=False,
        dry_run=False,
        definition_of_done=dod,
        browser=browser,
        url=url,
        gates=list(gates or []) or None,
        resume=None,
        show_diff=diff,
        allow_gate_failure=allow_failure,
        orchestrate=orchestrate,
    )


def fix_command(
    ctx: typer.Context,
    issue: str = COMMON_OPTIONS["task"],
    dod: str | None = typer.Option(None, "--dod", help="strict | balanced | advisory."),
    browser: bool | None = typer.Option(None, "--browser/--no-browser", help="Force browser validation on/off."),
    url: str | None = typer.Option(None, "--url", help="URL where the problem reproduces."),
    gates: list[str] = typer.Option(None, "--gate", help="Only run these gates (repeatable)."),
    diff: bool = typer.Option(True, "--diff/--no-diff", help="Show the working-tree diff at the end."),
    allow_failure: bool = typer.Option(False, "--allow-gate-failure", help="Exit 0 even when gates fail."),
    orchestrate: bool | None = typer.Option(
        None,
        "--orchestrate/--solo",
        # The bracket is escaped: Rich parses help strings as markup, so a bare
        # "[orchestration]" is swallowed as a style tag and the sentence reads
        # "decide from  and task complexity".
        help="Force multi-worker delegation on or off (default: decide from \[orchestration] and task complexity).",
    ),
) -> None:
    """Diagnose an issue, patch it, and prove it with tests/browser evidence.

    Examples:
      ua fix "checkout returns 500 when the cart is empty"
      ua fix "CI fails on the lint step" --gate lint
      ua fix "signup button does nothing" --url http://localhost:3000
    """
    task = (
        "Diagnose and fix the following issue. Reproduce it first, find the root cause, apply the smallest "
        "correct fix, then prove the fix with the project's tests and a browser check when the issue is "
        "user-visible. Do not weaken or delete tests to make them pass.\n\nIssue:\n"
        + _task_from_args(issue)
    )
    _execute(
        ctx,
        task=task,
        kind="fix",
        read_only=False,
        dry_run=False,
        definition_of_done=dod,
        browser=browser,
        url=url,
        gates=list(gates or []) or None,
        resume=None,
        show_diff=diff,
        allow_gate_failure=allow_failure,
        orchestrate=orchestrate,
    )


def plan_command(
    ctx: typer.Context,
    task: str = COMMON_OPTIONS["task"],
    plan_file: Path | None = typer.Option(None, "--out", help="Write the plan JSON to this file."),
    yes: bool = typer.Option(False, "--run", help="Approve the plan and execute it immediately."),
) -> None:
    """Produce a reviewable plan without touching a single file.

    Examples:
      ua plan "add Stripe checkout with webhooks"
      ua plan "migrate from SQLite to Postgres" --out plan.json
      ua plan "add rate limiting" --run
    """
    runtime = _runtime(ctx)
    if yes:
        runtime.assume_yes = True
        _execute(
            ctx,
            task=_task_from_args(task),
            kind="run",
            read_only=False,
            dry_run=False,
            definition_of_done=None,
            browser=None,
            url=None,
            gates=None,
            resume=None,
            show_diff=False,
            allow_gate_failure=False,
        )
        return

    runtime = _runtime(ctx)
    runtime.start_logging()
    task_text = _task_from_args(task)
    request = RunRequest(task=task_text, kind="plan", mode="plan", read_only=True, non_interactive=True)

    if not runtime.is_machine:
        header(f"plan :: {task_text[:100]}", subtitle="read-only planning session")

    result = runtime.execute_run(request)
    if plan_file and result.state.plan is not None:
        import json

        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(result.state.plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        output.diag_print(f"[ua.muted]plan written to {plan_file}[/ua.muted]")

    if runtime.is_machine:
        runtime.emit_json({"plan": result.state.plan.to_dict() if result.state.plan else None, "run_dir": str(result.run_dir)})
        return

    if result.state.plan is not None:
        output.blank()
        output.print(plan_tree(result.state.plan.steps, title=f"Plan: {result.state.plan.goal[:80]}"))
        for label, items in (("deliverables", result.state.plan.deliverables), ("verification", result.state.plan.verification), ("risks", result.state.plan.risks)):
            if items:
                output.print(f"\n[ua.muted]{label}[/ua.muted]")
                for item in items:
                    output.print(f"  • {item}")
        output.print(f"\n[ua.muted]execute with:[/ua.muted] ua run \"{task_text[:70]}\"")


def ask_command(
    ctx: typer.Context,
    question: str = COMMON_OPTIONS["task"],
    web: bool = typer.Option(False, "--web", help="Allow web fetch tools (off by default for questions)."),
) -> None:
    """Ask a read-only question about the codebase.

    Examples:
      ua ask "where is authentication implemented and how do sessions work?"
      ua ask "which tests cover the payments module?"
    """
    runtime = _runtime(ctx)
    runtime.start_logging()
    question_text = _task_from_args(question)
    loaded = runtime.load()
    loaded.settings.policy.read_only = True
    if not web:
        loaded.settings.policy.allow_network = False
    request = RunRequest(task=question_text, kind="ask", read_only=True, non_interactive=True)
    if not runtime.is_machine:
        header(f"ask :: {question_text[:100]}", subtitle="read-only session")
    result = runtime.execute_run(request)
    if runtime.is_machine:
        runtime.emit_json({"answer": result.state.summary, "run": result.state.to_dict()})
        return
    if result.state.summary:
        output.blank()
        output.print(result.state.summary)


__all__ = ["run_command", "build_command", "fix_command", "plan_command", "ask_command"]
