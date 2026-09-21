"""``ua runs`` - the run journal: what happened, with evidence.

Every run keeps its full record under ``.unknown/runs/<id>/``:

* ``events.jsonl``      the complete event stream (replayable, greppable)
* ``run.json``          final state: status, plan, budget, files, blockers
* ``plan.json``         the plan as executed (useful for `--resume`)
* ``verification.json`` gate evidence
* ``tools.json``        every tool call with arguments, duration and outcome
* ``diff.patch``        the working-tree diff captured at the end

That is what makes the agent auditable: a reviewer can reconstruct exactly what
it did, and why, without trusting the summary.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.cli.render import render_run_summary
from unknown_agent.cli.runtime import run_async
from unknown_agent.core.models import RunState
from unknown_agent.core.runner import RunRequest
from unknown_agent.errors import UsageError
from unknown_agent.observability.events import EventBus
from unknown_agent.ui.console import output
from unknown_agent.ui.prompts import confirm_destructive
from unknown_agent.ui.renderers import data_table, elapsed, gate_table, kv_table, unified_diff

app = typer.Typer(name="runs", help="Inspect and resume past runs.", no_args_is_help=True)


def _runs_dir(ctx: typer.Context) -> Path:
    runtime: Runtime = ctx.obj
    return runtime.paths.runs_dir


def _load_state(run_dir: Path) -> RunState | None:
    path = run_dir / "run.json"
    if not path.is_file():
        return None
    try:
        return RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_run(ctx: typer.Context, run_id: str) -> Path:
    directory = _runs_dir(ctx)
    if run_id in {"last", "--last"}:
        candidates = sorted((item for item in directory.glob("run-*") if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)
        if not candidates:
            raise UsageError("No runs recorded in this project yet.", hint="Start with: ua run \"<task>\"")
        return candidates[0]
    target = directory / run_id
    if not target.is_dir():
        matches = [item for item in directory.glob(f"*{run_id}*") if item.is_dir()]
        if len(matches) == 1:
            return matches[0]
        raise UsageError(f"No run matching {run_id!r}.", hint="List them with `ua runs list`.")
    return target


@app.command("list")
def list_runs(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", help="How many runs to show."),
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    failures_only: bool = typer.Option(False, "--failures", help="Only runs that did not complete."),
) -> None:
    """List recorded runs, newest first."""

    runtime: Runtime = ctx.obj
    directory = _runs_dir(ctx)
    entries: list[tuple[str, RunState | None]] = []
    for run_dir in sorted((item for item in directory.glob("run-*") if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True):
        state = _load_state(run_dir)
        if status and (state.status.value if state else "") != status:
            continue
        if failures_only and state and state.status.value == "completed":
            continue
        entries.append((run_dir.name, state))
        if len(entries) >= limit:
            break

    if runtime.is_machine:
        runtime.emit_json({"runs": [{"id": name, **(state.to_dict() if state else {})} for name, state in entries]})
        return
    if not entries:
        output.print("[ua.muted]no runs recorded[/ua.muted]")
        return
    rows = []
    for name, state in entries:
        if state is None:
            rows.append((name, "unknown", "-", "-", ""))
            continue
        rows.append(
            (
                name,
                state.status.value,
                elapsed(state.duration_s),
                f"{state.budget.steps_used} steps",
                state.task[:70],
            )
        )
    output.print(data_table(["run", "status", "duration", "steps", "task"], rows, title=f"runs ({len(rows)})"))


@app.command("show")
def show(
    ctx: typer.Context,
    run_id: str = typer.Argument("last", help="Run id, a unique substring, or 'last'."),
    events: bool = typer.Option(False, "--events", help="Print the event timeline."),
    tools: bool = typer.Option(False, "--tools", help="Print every tool call with arguments and outcome."),
    diff: bool = typer.Option(False, "--diff", help="Print the captured diff."),
    verification: bool = typer.Option(False, "--verification", help="Print gate evidence."),
    limit: int = typer.Option(80, "--limit", help="Maximum events to print."),
) -> None:
    """Inspect a run: state, plan, tool ledger, gate evidence and diff."""

    runtime: Runtime = ctx.obj
    run_dir = _resolve_run(ctx, run_id)
    state = _load_state(run_dir)

    if runtime.is_machine:
        payload: dict[str, object] = {"run_dir": str(run_dir), "state": state.to_dict() if state else None}
        if events:
            payload["events"] = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if tools:
            payload["tool_ledger"] = json.loads((run_dir / "tools.json").read_text(encoding="utf-8")) if (run_dir / "tools.json").is_file() else []
        if verification and (run_dir / "verification.json").is_file():
            payload["verification"] = json.loads((run_dir / "verification.json").read_text(encoding="utf-8"))
        runtime.emit_json(payload)
        return

    if state is None:
        output.print(f"[ua.warn]run.json missing in {run_dir}[/ua.warn]")
    else:
        render_run_summary(type("R", (), {"state": state, "diff": "", "run_dir": run_dir})(), show_diff=False)

    if verification and (run_dir / "verification.json").is_file():
        payload = json.loads((run_dir / "verification.json").read_text(encoding="utf-8"))
        from unknown_agent.core.models import GateResult

        results = [
            GateResult(
                name=item["name"],
                status=__import__("unknown_agent.core.models", fromlist=["GateStatus"]).GateStatus(item["status"]),
                command=item.get("command"),
                exit_code=item.get("exit_code"),
                duration_s=item.get("duration_s", 0.0),
                summary=item.get("summary", ""),
                output=item.get("output", ""),
                required=item.get("required", False),
                fix_hint=item.get("fix_hint"),
            )
            for item in payload.get("results", [])
        ]
        output.blank()
        output.print(gate_table(results))

    if tools and (run_dir / "tools.json").is_file():
        ledger = json.loads((run_dir / "tools.json").read_text(encoding="utf-8"))
        output.blank()
        output.print(
            data_table(
                ["tool", "risk", "ok", "ms", "arguments"],
                [
                    (entry["tool"], entry["risk"], entry["ok"], f"{entry['duration_ms']:.0f}", json.dumps(entry.get("arguments", {}))[:80])
                    for entry in ledger
                ],
                title=f"tool ledger ({len(ledger)} calls)",
            )
        )

    if diff:
        diff_path = run_dir / "diff.patch"
        if diff_path.is_file():
            output.blank()
            output.print(unified_diff(diff_path.read_text(encoding="utf-8")[:20_000]))

    if events:
        output.blank()
        output.print("[ua.muted]events[/ua.muted]")
        for index, event in enumerate(EventBus.read_journal(run_dir / "events.jsonl")):
            if index >= limit:
                output.print(f"[ua.muted]... {index} more[/ua.muted]")
                break
            stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
            output.print(f"[ua.dim]{stamp}[/ua.dim] {event.type:<18} {event.message[:120]}")


@app.command("resume")
def resume(
    ctx: typer.Context,
    run_id: str = typer.Argument("last", help="Run to resume."),
    task: str | None = typer.Option(None, "--task", help="Override the task (defaults to the original)."),
    extra: str | None = typer.Option(None, "--note", help="Additional instruction for the resumed run."),
) -> None:
    """Resume a run, keeping its plan and journal history.

    Examples:
      ua runs resume last
      ua runs resume last --note "focus on the browser failure"
    """
    runtime: Runtime = ctx.obj
    run_dir = _resolve_run(ctx, run_id)
    state = _load_state(run_dir)
    if state is None:
        raise UsageError(f"Run {run_dir.name} has no recorded state.")
    task_text = task or state.task
    if extra:
        task_text = f"{task_text}\n\nAdditional instruction: {extra}"
    request = RunRequest(
        task=task_text,
        kind="run",
        mode=state.mode if state.mode in {"plan", "react"} else "auto",
        resume_run_id=run_dir.name,
        non_interactive=runtime.non_interactive,
        yes=runtime.assume_yes,
    )
    output.print(f"[ua.info]resuming[/ua.info] {run_dir.name}: {task_text[:100]}")
    result = runtime.execute_run(request)
    if runtime.is_machine:
        runtime.emit_json(result.to_dict())
        return
    render_run_summary(result, show_diff=False)


@app.command("prune")
def prune(
    ctx: typer.Context,
    keep: int = typer.Option(20, "--keep", help="How many recent runs to keep."),
    days: int | None = typer.Option(None, "--days", help="Also delete runs older than this many days."),
) -> None:
    """Delete old run directories (keeps the newest ``--keep``)."""

    runtime: Runtime = ctx.obj
    directory = _runs_dir(ctx)
    runs = sorted((item for item in directory.glob("run-*") if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)
    doomed = runs[keep:]
    if days:
        cutoff = time.time() - days * 86_400
        doomed.extend([item for item in runs[:keep] if item.stat().st_mtime < cutoff])
    doomed = sorted(set(doomed))
    if not doomed:
        output.print("[ua.muted]nothing to prune[/ua.muted]")
        return
    if not runtime.assume_yes and not confirm_destructive(runtime.prompter, f"delete {len(doomed)} run director{'y' if len(doomed) == 1 else 'ies'}", preview="\n".join(item.name for item in doomed[:10])):
        output.print("[ua.muted]cancelled[/ua.muted]")
        return
    for item in doomed:
        shutil.rmtree(item, ignore_errors=True)
    output.print(f"[ua.ok]pruned[/ua.ok] {len(doomed)} run(s)")


@app.command("path")
def path_cmd(ctx: typer.Context, run_id: str = typer.Argument("last")) -> None:
    """Print the directory of a run (for `cd $(ua runs path last)`)."""

    output.raw(str(_resolve_run(ctx, run_id)))


__all__ = ["app"]
