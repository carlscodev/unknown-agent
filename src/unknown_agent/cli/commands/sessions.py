"""``ua sessions`` - the cross-project index of runs."""

from __future__ import annotations

import time

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.memory.sessions import SessionStore
from unknown_agent.ui.console import output
from unknown_agent.ui.prompts import confirm_destructive
from unknown_agent.ui.renderers import data_table, elapsed, kv_table

app = typer.Typer(name="sessions", help="Session index: every run, across every project on this machine.", no_args_is_help=True)


def _store(ctx: typer.Context) -> tuple[Runtime, SessionStore]:
    runtime: Runtime = ctx.obj
    return runtime, SessionStore(runtime.paths.sessions_db)


@app.command("list")
def list_sessions(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit"),
    all_projects: bool = typer.Option(False, "--all", help="Include runs from other project directories."),
    status: str | None = typer.Option(None, "--status"),
    search: str | None = typer.Option(None, "--search", help="Substring match on task, summary or id."),
) -> None:
    """List sessions (this project by default)."""

    runtime, store = _store(ctx)
    try:
        cwd = None if all_projects else str(runtime.paths.cwd)
        records = store.list(limit=limit, status=status, cwd=cwd, search=search)
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json({"sessions": [record.to_dict() for record in records]})
        return
    if not records:
        output.print("[ua.muted]no sessions recorded[/ua.muted]")
        return
    rows = [
        (
            record.id,
            record.status,
            elapsed(record.duration_s),
            f"{record.files_changed} files",
            f"{record.gates_passed}/{record.gates_passed + record.gates_failed} gates",
            record.task[:60],
        )
        for record in records
    ]
    output.print(data_table(["session", "status", "duration", "changes", "gates", "task"], rows, title=f"sessions ({len(rows)})"))


@app.command("show")
def show(ctx: typer.Context, session_id: str = typer.Argument("last", help="Session id, unique substring, or 'last'.")) -> None:
    """Show one session in detail."""

    runtime, store = _store(ctx)
    try:
        record = None
        if session_id in {"last", "--last"}:
            record = store.last(cwd=str(runtime.paths.cwd)) or store.last()
        else:
            record = store.get(session_id) or next((item for item in store.list(limit=500) if session_id in item.id), None)
        if record is None:
            raise UsageError(f"No session matching {session_id!r}.", hint="List them with `ua sessions list`.")
        payload = record.to_dict()
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    rows = [
        ("id", payload["id"]),
        ("status", payload["status"]),
        ("task", payload["task"][:160]),
        ("mode", payload["mode"]),
        ("model", f"{payload['provider']}/{payload['model']}"),
        ("created", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payload["created_at"]))),
        ("duration", elapsed(payload["duration_s"])),
        ("steps", payload["step"]),
        ("files changed", payload["files_changed"]),
        ("gates", f"{payload['gates_passed']} passed, {payload['gates_failed']} failed"),
        ("blockers", payload["blockers"] or "none"),
        ("run directory", payload["run_path"] or "n/a"),
        ("summary", payload["summary"][:400]),
    ]
    output.print(kv_table(rows, title="session"))


@app.command("rm")
def rm(ctx: typer.Context, session_id: str = typer.Argument(..., help="Session id to delete from the index.")) -> None:
    """Remove a session from the index (does not delete the run directory)."""

    runtime, store = _store(ctx)
    try:
        record = store.get(session_id)
        if record is None:
            raise UsageError(f"No session with id {session_id!r}.")
        if not runtime.assume_yes and not confirm_destructive(runtime.prompter, f"remove session {session_id} from the index"):
            output.print("[ua.muted]cancelled[/ua.muted]")
            return
        store.delete(session_id)
    finally:
        store.close()
    output.print(f"[ua.ok]removed[/ua.ok] {session_id}")


@app.command("stats")
def stats(ctx: typer.Context) -> None:
    """Session counts and database size."""

    runtime, store = _store(ctx)
    try:
        payload = store.stats()
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(kv_table([("sessions", payload["sessions"]), ("by status", payload["by_status"] or "none"), ("database", f"{payload['database_bytes'] / 1024:.0f} KiB"), ("path", payload["path"])], title="session stats"))


@app.command("prune")
def prune(ctx: typer.Context, keep: int = typer.Option(200, "--keep", help="How many sessions to keep.")) -> None:
    """Trim the session index."""

    runtime, store = _store(ctx)
    try:
        removed = store.prune(keep)
    finally:
        store.close()
    output.print(f"[ua.ok]pruned[/ua.ok] {removed} session record(s)")


@app.command("export")
def export(ctx: typer.Context, out: str = typer.Option("sessions.json", "--out")) -> None:
    """Export the session index as JSON."""

    from pathlib import Path

    runtime, store = _store(ctx)
    try:
        count = store.export_json(Path(out))
    finally:
        store.close()
    output.print(f"[ua.ok]exported[/ua.ok] {count} sessions to {out}")


__all__ = ["app"]
