"""``ua orchestrate`` — plan, run and audit multi-worker delegation.

This is the product surface for :mod:`unknown_agent.orchestration`.  Three jobs:

``plan``   Show the work graph a request *would* produce — roles, dependencies,
           ownership, acceptance criteria, and what the budget trimmed — without
           delegating anything and without writing an audit file.
``run``    Delegate for real, streaming each dispatch and gate to the terminal.
``roles`` / ``list`` / ``show`` / ``ledger``
           Explain the specialists, and answer "why did the agent do that?" from
           the JSONL trail on disk.

Everything honours ``--json`` (machine mode) so CI can assert on a plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.text import Text
from rich.tree import Tree

from unknown_agent.cli.runtime import Runtime, run_async
from unknown_agent.errors import ExitCode
from unknown_agent.orchestration import (
    ROLES,
    AgentExecutor,
    WorkGraph,
    WorkItem,
    analyze_task,
    get_role,
    ledger_path_for,
    list_ledgers,
    orchestrate,
    read_ledger,
    roles_payload,
    timeline,
)
from unknown_agent.orchestration.worker import OfflineExecutor, WorkerContext
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table, status_label, summary_panel

app = typer.Typer(
    name="orchestrate",
    help="Multi-worker delegation: plan a shape, run it, audit it afterwards.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

#: Glyphs per role, so a graph reads as a picture rather than a list of ids.
_GLYPHS = {
    "planner": "◈",
    "inspector": "⌕",
    "coder": "▶",
    "refactorer": "⇄",
    "ux-finisher": "✦",
    "browser-validator": "◎",
    "verifier": "✓",
    "documenter": "≡",
    "releaser": "⬆",
    "tooling": "⚒",
}

_STATE_STYLE = {
    "succeeded": "ua.ok",
    "failed": "ua.err",
    "blocked": "ua.err",
    "cancelled": "ua.dim",
    "skipped": "ua.dim",
    "running": "ua.accent",
    "queued": "ua.accent",
    "pending": "ua.dim",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _runtime(ctx: typer.Context) -> Runtime:
    runtime: Runtime = ctx.obj
    runtime.start_logging()
    return runtime


def _root(runtime: Runtime, loaded: Any) -> Path:
    return Path(loaded.project_root or loaded.paths.cwd)


def _config(loaded: Any, *, max_workers: int | None, concurrency: int | None, pattern: str | None) -> Any:
    """The session's orchestration config with CLI flags layered on top."""

    config = loaded.settings.orchestration.model_copy(deep=True)
    if max_workers is not None:
        config.max_workers = max_workers
    if concurrency is not None:
        config.concurrency = concurrency
    if pattern:
        config.pattern = pattern  # type: ignore[assignment]
    return config


def _glyph(role_id: str) -> str:
    return get_role(role_id).glyph or _GLYPHS.get(role_id, "·")


def _graph_tree(graph: WorkGraph, *, show_acceptance: bool = False) -> Tree:
    """The graph as a dependency tree: what waits on what, and who owns which paths."""

    tree = Tree(
        Text.assemble(
            (f"{graph.pattern} ", "bold ua.accent"),
            (f"· {len(graph.items)} workers", "ua.dim"),
        )
    )
    nodes: dict[str, Tree] = {}
    for item in sorted(graph.items, key=lambda entry: (get_role(entry.role).stage, entry.created_at)):
        label = Text()
        label.append(f"{_glyph(item.role)} ", style="ua.accent")
        label.append(get_role(item.role).label, style="bold")
        if not item.required:
            label.append(" (optional)", style="ua.dim")
        node = tree.add(label)
        detail = Text(item.task[:110], style="ua.dim")
        node.add(detail)
        if item.owned_paths:
            node.add(Text("owns " + ", ".join(item.owned_paths[:4]), style="ua.dim"))
        if show_acceptance and item.acceptance:
            for criterion in item.acceptance[:3]:
                node.add(Text(f"· {criterion}", style="ua.obs"))
        nodes[item.id] = node
    return tree


def _item_rows(graph: WorkGraph) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for item in graph.items:
        role = get_role(item.role)
        rows.append(
            (
                Text(f"{_glyph(item.role)} {role.label}"),
                Text(item.state.value, style=_STATE_STYLE.get(item.state.value, "")),
                Text("required" if item.required else "optional", style="" if item.required else "ua.dim"),
                Text(", ".join(item.owned_paths[:2]) or "—", style="ua.dim"),
                Text(", ".join(role.verify_gates) or "—", style="ua.dim"),
                Text(item.id, style="ua.dim"),
            )
        )
    return rows


def _executor(name: str, loaded: Any = None, *, model: str = "", provider: str = "", dry_run: bool = False) -> Any:
    """Build the executor a command asked for.

    ``agent`` is the real one: each worker becomes a bounded run of the core loop
    with its role's tools, gates and step limit.  ``offline`` is deterministic and
    calls no model, which is what you want when you are testing a *shape* rather
    than asking for code — so it says so in the summary line it produces.
    """

    if name == "offline":
        return OfflineExecutor()
    if name == "agent":
        if loaded is None:
            raise typer.BadParameter("the agent executor needs a loaded configuration")
        return AgentExecutor(
            loaded,
            model=model or None,
            provider=provider or None,
            dry_run=dry_run,
        )
    raise typer.BadParameter(f"unknown executor '{name}' (available: agent, offline)")


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
@app.command("plan")
def plan_command(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="The request to plan delegation for."),
    pattern: str = typer.Option("", "--pattern", help="Force a shape: solo, pipeline, fanout, graph, escalate."),
    max_workers: int = typer.Option(None, "--max-workers", help="Cap the number of workers."),
    acceptance: bool = typer.Option(False, "--acceptance", help="Show each worker's acceptance criteria."),
) -> None:
    """Show the work graph a request would produce, without delegating anything.

    Nothing is executed and no audit file is written: a plan is a preview.

    Examples:
      ua orchestrate plan "build a checkout flow with cart, payments and docs"
      ua orchestrate plan "refactor auth to use DI" --pattern pipeline
      ua orchestrate plan "ship the landing page" --json
    """

    runtime = _runtime(ctx)
    loaded = runtime.load()
    config = _config(loaded, max_workers=max_workers, concurrency=None, pattern=pattern or None)
    outcome = run_async(
        orchestrate(
            task,
            config=config,
            executor=OfflineExecutor(),  # a plan delegates nothing; no model is called
            project_root=str(_root(runtime, loaded)),
            dry_run=True,
        )
    )
    graph = outcome.graph

    if runtime.is_machine:
        runtime.emit_json(
            {
                "task": task,
                "analysis": analyze_task(task).to_dict() if hasattr(analyze_task(task), "to_dict") else {},
                "pattern": graph.pattern,
                "rationale": graph.rationale,
                "ok": outcome.ok,
                "blockers": outcome.blockers,
                "warnings": graph.warnings,
                "graph": graph.to_dict(),
            }
        )
        raise typer.Exit(ExitCode.OK if outcome.ok else ExitCode.BLOCKED)

    analysis = analyze_task(task)
    output.blank()
    output.print(
        summary_panel(
            "orchestration plan",
            [
                ("shape", graph.pattern),
                ("why", graph.rationale),
                ("complexity", f"{analysis.complexity}/10"),
                ("underspecified", f"{analysis.underspecification:.2f}"),
                ("workers", len(graph.items)),
                ("surfaces", ", ".join(analysis.surfaces) or "none detected"),
            ],
        )
    )
    output.blank()
    output.print(_graph_tree(graph, show_acceptance=acceptance))
    output.blank()
    output.print(
        data_table(
            ["worker", "state", "kind", "owns", "verified by", "id"],
            _item_rows(graph),
            title="work graph",
        )
    )
    for warning in graph.warnings:
        output.print(f"[ua.warn]![/ua.warn] {warning}")
    for reason in analysis.reasons:
        output.print(f"[ua.dim]· {reason}[/ua.dim]")
    if not outcome.ok:
        output.blank()
        for blocker in outcome.blockers:
            output.print(f"[ua.err]✗[/ua.err] {blocker}")
        raise typer.Exit(ExitCode.BLOCKED)
    output.blank()


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
@app.command("run")
def run_command(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="The request to delegate."),
    pattern: str = typer.Option("", "--pattern", help="Force a shape instead of letting the router choose."),
    max_workers: int = typer.Option(None, "--max-workers", help="Cap the number of workers."),
    concurrency: int = typer.Option(None, "--concurrency", help="Workers executing at the same time."),
    executor: str = typer.Option(
        "agent", "--executor", help="Executor: agent (real bounded runs) or offline (deterministic, no model)."
    ),
    model: str = typer.Option("", "--model", help="Model override for every worker run."),
    provider: str = typer.Option("", "--provider", help="Provider profile override for every worker run."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; delegate nothing."),
    run_id: str = typer.Option("", "--run-id", help="Run id for the audit ledger."),
) -> None:
    """Delegate a request to specialised workers and report what each produced.

    Exit codes: 0 completed, 3 blocked (a required worker or gate failed),
    8 budget exhausted.

    Examples:
      ua orchestrate run "build the checkout and its docs"
      ua orchestrate run "refactor auth" --pattern pipeline --max-workers 4
      ua orchestrate run "build the cart" --executor offline   # no model call
      ua orchestrate run "ship it" --dry-run --json
    """

    runtime = _runtime(ctx)
    loaded = runtime.load()
    root = _root(runtime, loaded)
    config = _config(loaded, max_workers=max_workers, concurrency=concurrency, pattern=pattern or None)
    worker = _executor(executor, loaded, model=model, provider=provider, dry_run=dry_run)

    events: list[tuple[str, dict[str, Any]]] = []

    def on_event(kind: str, payload: Any) -> None:
        data = dict(payload or {})
        events.append((kind, data))
        if runtime.is_machine or runtime.quiet:
            return
        role = str(data.get("role") or "")
        work_id = str(data.get("work_id") or "")
        label = get_role(role).label if role in ROLES else role
        glyph = _glyph(role) if role in ROLES else "·"
        if kind == "run_started":
            output.print(f"[ua.accent]{glyph} {data.get('pattern', '')}[/ua.accent] [ua.dim]{data.get('items', '')} workers[/ua.dim]")
        elif kind == "worker_started":
            output.print(f"[ua.dim]{glyph} {label or work_id}: started[/ua.dim]")
        elif kind == "worker_finished":
            ok = data.get("ok")
            output.print(Text.assemble((f"{glyph} {label or work_id}: ", ""), (status_label(bool(ok)).plain, "ua.ok" if ok else "ua.err")))
        elif kind in {"work_failed", "work_blocked"}:
            output.print(f"[ua.err]✗ {label or work_id}: {data.get('error') or data.get('reason') or kind}[/ua.err]")
        elif kind == "work_retried":
            output.print(f"[ua.warn]↻ {label or work_id}: retrying ({data.get('error') or data.get('reason') or 'failure'})[/ua.warn]")
        elif kind == "merge_conflict":
            output.print(f"[ua.err]✗ merge conflict: {data.get('conflicts')}[/ua.err]")
        elif kind == "budget_exhausted":
            output.print(f"[ua.warn]![/ua.warn] budget exhausted: {data.get('note', '')}")

    outcome = run_async(
        orchestrate(
            task,
            config=config,
            executor=worker,
            project_root=str(root),
            on_event=on_event,
            run_id=run_id,
            dry_run=dry_run,
        )
    )

    if runtime.is_machine:
        runtime.emit_json(outcome.to_dict())
    else:
        output.blank()
        output.print(
            summary_panel(
                f"orchestration {outcome.status}",
                [
                    ("run", outcome.run_id),
                    ("shape", outcome.pattern),
                    ("workers", f"{len(outcome.succeeded())}/{len(outcome.graph.items)} succeeded"),
                    ("retries", outcome.retries),
                    ("escalations", outcome.escalations),
                    ("duration", f"{outcome.duration_s}s"),
                    ("ledger", str(ledger_path_for(root, outcome.run_id)) if not dry_run else "not written (dry run)"),
                ],
            )
        )
        if outcome.merge is not None:
            output.blank()
            output.print(
                kv_table(
                    [
                        ("merge", outcome.merge.strategy),
                        ("files", ", ".join(outcome.merge.files[:6]) or "none"),
                        ("conflicts", len(outcome.merge.conflicts)),
                        ("notes", "; ".join(outcome.merge.notes[:3]) or "none"),
                    ],
                    title="merge",
                )
            )
        if outcome.unverified:
            output.print(
                f"[ua.warn]![/ua.warn] [ua.warn]{len(outcome.unverified)} workers' gates were not verified — "
                "no verifier was wired to this run.[/ua.warn]"
            )
        for blocker in outcome.blockers:
            output.print(f"[ua.err]✗[/ua.err] {blocker}")
        output.print(f"\n{outcome.summary}")

    # Exhaustion only matters when it *stopped* work.  Spending the whole worker
    # budget on a run that completed is the budget being used, not exceeded, so
    # ``budget.exhausted()`` alone must not decide the exit code.
    if outcome.status == "cancelled":
        raise typer.Exit(ExitCode.BUDGET)
    raise typer.Exit(ExitCode.OK if outcome.ok else ExitCode.BLOCKED)


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #
@app.command("roles")
def roles_command(
    ctx: typer.Context,
    role: str = typer.Argument("", help="One role id, for the full description."),
) -> None:
    """List the ten specialists: mission, tools, boundaries, gates, stage.

    Examples:
      ua orchestrate roles
      ua orchestrate roles coder
      ua orchestrate roles --json
    """

    runtime = _runtime(ctx)
    if role:
        from unknown_agent.orchestration import resolve_role_id

        resolved = resolve_role_id(role)
        if resolved is None:
            output.print(f"[ua.err]✗[/ua.err] unknown role '{role}'. Known: {', '.join(ROLES)}")
            raise typer.Exit(ExitCode.USAGE)
        entry = get_role(resolved)
        if runtime.is_machine:
            runtime.emit_json({**roles_payload()[list(ROLES).index(resolved)], "tools": list(entry.tools)})
            return
        output.blank()
        output.print(
            summary_panel(
                f"{_glyph(resolved)} {entry.label}",
                [
                    ("id", entry.id),
                    ("mission", entry.mission),
                    ("stage", entry.stage),
                    ("mutates", "yes" if entry.mutates else "no"),
                    ("required", "yes" if entry.required else "no"),
                    ("can delegate", "yes" if entry.can_delegate else "no"),
                    ("reasoning", entry.reasoning_mode),
                    ("max steps", entry.max_steps),
                ],
            )
        )
        output.blank()
        output.print(
            kv_table(
                [
                    ("outputs", list(entry.outputs)),
                    ("tools", list(entry.tools)),
                    ("denied", list(entry.deny_tools) or "none"),
                    ("owns", list(entry.owned_paths) or "nothing"),
                    ("verified by", list(entry.verify_gates)),
                    ("guidance", entry.guidance),
                ]
            )
        )
        output.blank()
        return

    payload = roles_payload()
    if runtime.is_machine:
        runtime.emit_json({"roles": payload})
        return
    rows = [
        (
            Text(f"{_glyph(entry['id'])} {entry['label']}"),
            entry["stage"],
            Text("mutates" if entry["mutates"] else "read-only", style="ua.warn" if entry["mutates"] else "ua.dim"),
            Text(", ".join(entry["tools"][:3]) + ("…" if len(entry["tools"]) > 3 else ""), style="ua.dim"),
            Text(", ".join(entry["deny_tools"][:2]) or "—", style="ua.dim"),
            Text(", ".join(entry["verify_gates"]) or "—", style="ua.dim"),
            Text("required" if entry["required"] else "optional", style="" if entry["required"] else "ua.dim"),
        )
        for entry in payload
    ]
    output.blank()
    output.print(data_table(["role", "stage", "writes", "tools", "denied", "verified by", "kind"], rows, title="specialists"))
    output.blank()


# --------------------------------------------------------------------------- #
# audit trail
# --------------------------------------------------------------------------- #
@app.command("list")
def list_command(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", help="How many runs to show."),
) -> None:
    """List past orchestration runs from the on-disk ledger."""

    runtime = _runtime(ctx)
    loaded = runtime.load()
    root = _root(runtime, loaded)
    entries = list_ledgers(root, limit=limit)
    if runtime.is_machine:
        runtime.emit_json({"runs": entries})
        return
    if not entries:
        output.print("[ua.dim]No orchestration runs recorded yet in .unknown/orchestration/.[/ua.dim]")
        return
    rows = [
        (
            Text(entry["run_id"], style="ua.accent"),
            entry["size"],
            Path(entry["path"]).name,
        )
        for entry in entries
    ]
    output.blank()
    output.print(data_table(["run", "bytes", "file"], rows, title=f"orchestration runs ({len(entries)})"))
    output.blank()


@app.command("show")
def show_command(
    ctx: typer.Context,
    run: str = typer.Argument("", help="Run id (default: the most recent)."),
) -> None:
    """Replay one run: the plan, every dispatch, and how it ended."""

    runtime = _runtime(ctx)
    loaded = runtime.load()
    root = _root(runtime, loaded)
    entries = list_ledgers(root, limit=50)
    if not entries:
        output.print("[ua.err]✗[/ua.err] no orchestration runs recorded.")
        raise typer.Exit(ExitCode.USAGE)
    chosen = next((entry for entry in entries if entry["run_id"] == run), None) if run else entries[0]
    if chosen is None:
        output.print(f"[ua.err]✗[/ua.err] no run '{run}'. Known: {', '.join(entry['run_id'] for entry in entries[:8])}")
        raise typer.Exit(ExitCode.USAGE)
    events = read_ledger(chosen["path"])
    if runtime.is_machine:
        runtime.emit_json({"run_id": chosen["run_id"], "events": [event.to_dict() for event in events]})
        return
    output.blank()
    for line in timeline(events):
        output.print(line)
    output.blank()


@app.command("ledger")
def ledger_command(
    ctx: typer.Context,
    run: str = typer.Argument("", help="Run id (default: the most recent)."),
    kind: str = typer.Option("", "--kind", help="Only events of this kind."),
    path: str = typer.Option("", "--path", help="Read a specific ledger file."),
) -> None:
    """Print the raw JSONL audit trail (pipe it into jq).

    Examples:
      ua orchestrate ledger
      ua orchestrate ledger --kind work_failed
      ua orchestrate ledger orc-20260101-120000-ab12 --json
    """

    runtime = _runtime(ctx)
    loaded = runtime.load()
    root = _root(runtime, loaded)
    target = Path(path) if path else None
    if target is None:
        entries = list_ledgers(root, limit=50)
        if not entries:
            output.print("[ua.err]✗[/ua.err] no orchestration runs recorded.")
            raise typer.Exit(ExitCode.USAGE)
        chosen = next((entry for entry in entries if entry["run_id"] == run), None) if run else entries[0]
        if chosen is None:
            output.print(f"[ua.err]✗[/ua.err] no run '{run}'.")
            raise typer.Exit(ExitCode.USAGE)
        target = Path(chosen["path"])
    events = read_ledger(target)
    if kind:
        events = [event for event in events if event.kind == kind]
    if runtime.is_machine:
        runtime.emit_json({"path": str(target), "events": [event.to_dict() for event in events]})
        return
    for event in events:
        output.raw(json.dumps(event.to_dict(), ensure_ascii=False, default=str))


@app.command("worker")
def worker_command(
    ctx: typer.Context,
    role: str = typer.Argument(..., help="Role id to run alone (see `ua orchestrate roles`)."),
    task: str = typer.Argument(..., help="What that worker should do."),
    executor: str = typer.Option(
        "agent", "--executor", help="Executor: agent (real bounded run) or offline (deterministic)."
    ),
    model: str = typer.Option("", "--model", help="Model override for this worker run."),
    provider: str = typer.Option("", "--provider", help="Provider profile override for this worker run."),
) -> None:
    """Run a single specialist on its own — the unit the conductor dispatches.

    Useful for checking a role's prompt, tool subset and boundaries in isolation.

    Examples:
      ua orchestrate worker inspector "map this repository"
      ua orchestrate worker coder "add a healthcheck endpoint" --json
      ua orchestrate worker verifier "prove the gates are green" --executor offline
    """

    from unknown_agent.orchestration import resolve_role_id
    from unknown_agent.orchestration.worker import Worker

    runtime = _runtime(ctx)
    loaded = runtime.load()
    resolved = resolve_role_id(role)
    if resolved is None:
        output.print(f"[ua.err]✗[/ua.err] unknown role '{role}'. Known: {', '.join(ROLES)}")
        raise typer.Exit(ExitCode.USAGE)
    entry = get_role(resolved)
    item = WorkItem(id="cli-1", role=resolved, task=task, owned_paths=list(entry.owned_paths))
    context = WorkerContext(project_root=str(_root(runtime, loaded)), task=task)
    worker = Worker(
        resolved, executor=_executor(executor, loaded, model=model, provider=provider), context=context
    )
    result = run_async(worker.run(item))

    if runtime.is_machine:
        runtime.emit_json(
            {
                "role": resolved,
                "tools": worker.tool_subset(list(entry.tools)),
                "prompt_chars": len(worker.build_prompt(item)),
                "result": result.to_dict(),
            }
        )
        raise typer.Exit(ExitCode.OK if result.ok else ExitCode.FAILURE)

    output.blank()
    output.print(
        summary_panel(
            f"{_glyph(resolved)} {entry.label}",
            [
                ("mission", entry.mission),
                ("tools", ", ".join(worker.tool_subset(list(entry.tools))) or "none"),
                ("denied", ", ".join(entry.deny_tools) or "none"),
                ("owns", ", ".join(entry.owned_paths) or "nothing"),
                ("verified by", ", ".join(entry.verify_gates)),
                ("prompt", f"{len(worker.build_prompt(item))} chars"),
                ("result", result.summary or result.error or "no output"),
                ("ok", status_label(result.ok).plain),
            ],
        )
    )
    output.blank()
    raise typer.Exit(ExitCode.OK if result.ok else ExitCode.FAILURE)


__all__ = ["app"]
