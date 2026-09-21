"""``ua sessions`` - the cross-project index of runs + TUI chat history."""

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
    tui: bool = typer.Option(False, "--tui", help="List TUI chat sessions instead of runs (polished history)."),
) -> None:
    """List sessions (this project by default).  Add ``--tui`` for chat history."""

    runtime: Runtime = ctx.obj
    if tui:
        from unknown_agent.tui.history import TuiSessionStore

        db_path = runtime.paths.state_dir / "tui_sessions.db"
        transcripts_dir = runtime.paths.state_dir / "tui_transcripts"
        store = TuiSessionStore(db_path, transcripts_dir)
        try:
            cwd = None if all_projects else str(runtime.paths.cwd)
            records = store.list(limit=limit, cwd=cwd, search=search)
            if status:
                records = [r for r in records if r.status == status]
            try:
                # For a filtered view, "current" should be the most recent session
                # in that scope, not the global marker from a different project.
                if cwd:
                    latest_here = store.latest(cwd=cwd)
                    current = latest_here.id if latest_here else store.get_current_marker()
                else:
                    current = store.get_current_marker()
            except Exception:
                current = None
        finally:
            store.close()
        if runtime.is_machine:
            runtime.emit_json({"sessions": [r.to_dict() for r in records], "current": current})
            return
        if not records:
            output.print("[ua.muted]no tui sessions yet — launch with `ua tui`[/ua.muted]")
            return
        rows = []
        for rec in records:
            cur = "● current" if rec.id == current else rec.status
            rows.append(
                (
                    rec.display_name[:34],
                    rec.short_id,
                    rec.date_str,
                    rec.time_str,
                    f"{(rec.provider or '—')}/{(rec.model or '—')}"[:28],
                    f"{rec.turns}τ {rec.message_count}✉",
                    cur,
                )
            )
        output.print(data_table(["name", "id", "date", "time", "provider/model", "activity", "status"], rows, title=f"tui sessions ({len(rows)})"))
        output.blank()
        output.print("[ua.muted]resume:[/ua.muted] ua tui --resume <id>  [ua.dim](or 'latest')[/ua.dim]    [ua.muted]inside TUI:[/ua.muted] /sessions  f7  ctrl+o")
        return

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
        output.print("[ua.muted]hint:[/ua.muted] try `ua sessions list --tui` for chat history")
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


# --------------------------------------------------------------------------- #
# TUI chat history — polished list + resume picker (matches other CLIs)
# --------------------------------------------------------------------------- #
@app.command("tui")
def tui_history(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit"),
    all_projects: bool = typer.Option(False, "--all", help="Include sessions from other project directories."),
    search: str | None = typer.Option(None, "--search", help="Filter by name, id, provider, or task."),
) -> None:
    """Polished TUI chat history — name, ID, date, time, provider/model, turns, status.

    Alias for ``ua sessions list --tui`` with a focused view.  Current session is
    marked ``● current`` and every row shows exactly how to resume it.
    """

    runtime: Runtime = ctx.obj
    from unknown_agent.tui.history import TuiSessionStore

    db_path = runtime.paths.state_dir / "tui_sessions.db"
    transcripts_dir = runtime.paths.state_dir / "tui_transcripts"
    store = TuiSessionStore(db_path, transcripts_dir)
    try:
        cwd = None if all_projects else str(runtime.paths.cwd)
        records = store.list(limit=limit, cwd=cwd, search=search)
        try:
            if cwd:
                latest_here = store.latest(cwd=cwd)
                current = latest_here.id if latest_here else store.get_current_marker()
            else:
                current = store.get_current_marker()
        except Exception:
            current = None
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json({"sessions": [r.to_dict() for r in records], "current": current, "current_record": next((r.to_dict() for r in records if r.id == current), None)})
        return
    if not records:
        output.print("[ua.muted]no tui sessions yet — launch with `ua tui`[/ua.muted]")
        return
    rows = []
    for rec in records:
        cur = "● current" if rec.id == current else rec.status
        # Highlight current row via plain marker; data_table will style the header
        rows.append(
            (
                rec.display_name[:34],
                rec.short_id,
                rec.date_str,
                rec.time_str,
                f"{(rec.provider or '—')}/{(rec.model or '—')}"[:28],
                f"{rec.turns}τ {rec.message_count}✉",
                cur,
            )
        )
    output.print(data_table(["name", "id", "date", "time", "provider/model", "activity", "status"], rows, title=f"tui sessions ({len(rows)})"))
    if current:
        try:
            cur_rec = next((r for r in records if r.id == current), None)
            if cur_rec:
                output.blank()
                output.print(f"[ua.muted]current:[/ua.muted] {cur_rec.display_name} ({cur_rec.short_id})  {cur_rec.datetime_str}  — resume with [ua.accent]ua tui --resume {cur_rec.short_id}[/ua.accent]")
        except Exception:
            pass
    output.blank()
    output.print("[ua.muted]resume:[/ua.muted] ua tui --resume <id>  ·  ua sessions resume <id>  ·  inside TUI: /sessions  f7")


@app.command("resume")
def resume_session(
    ctx: typer.Context,
    session_id: str = typer.Argument("latest", help="Session id, short id, or 'latest'/'last'."),
    launch: bool = typer.Option(True, "--launch/--no-launch", help="Launch the TUI resumed to this session (default) or just print the command."),
) -> None:
    """Resume a TUI session by id — prints the exact command and optionally launches.

    ``ua sessions resume`` is the CLI twin of the in-TUI picker (f7 / /sessions).
    It resolves short ids, validates the target, and shows the same resume command
    that is printed on every exit.
    """

    runtime: Runtime = ctx.obj
    from unknown_agent.tui.history import TuiSessionStore

    db_path = runtime.paths.state_dir / "tui_sessions.db"
    transcripts_dir = runtime.paths.state_dir / "tui_transcripts"
    store = TuiSessionStore(db_path, transcripts_dir)
    try:
        # Resolve "latest"/"last" or id substring
        target = None
        if session_id in {"latest", "last", "--last"}:
            target = store.latest()
            if target is None:
                output.print("[ua.warn]no tui sessions to resume[/ua.warn]")
                raise typer.Exit(1)
        else:
            target = store.get(session_id)
            if target is None:
                # Try listing hint
                recent = store.list(limit=5)
                output.print(f"[ua.warn]no tui session matching {session_id!r}[/ua.warn]")
                if recent:
                    output.print("[ua.muted]recent:[/ua.muted]")
                    for rec in recent[:3]:
                        output.print(f"  {rec.short_id}  {rec.display_name}  {rec.datetime_str}  {rec.provider}/{rec.model}")
                raise typer.Exit(1)
        payload = target.to_dict()
        if runtime.is_machine:
            runtime.emit_json(payload)
            return
        # Polished metadata display — not raw logs
        rows = [
            ("name", payload["display_name"]),
            ("id", f"{payload['id']} ({payload['short_id']})"),
            ("date", payload["date"]),
            ("time", payload["time"]),
            ("datetime", payload["datetime"]),
            ("status", payload["status"]),
            ("cwd", payload["cwd"] or "—"),
            ("provider/model", f"{payload['provider'] or '—'}/{payload['model'] or '—'}"),
            ("turns", str(payload["turns"])),
            ("messages", str(payload["message_count"])),
            ("resumes", str(payload["resume_count"])),
            ("last task", (payload["last_task"][:120] + "…") if len(payload["last_task"] or "") > 120 else (payload["last_task"] or "—")),
        ]
        output.print(kv_table(rows, title="tui session"))
        output.blank()
        cmd = f"ua tui --resume {payload['short_id']}"
        alt = f"ua sessions resume {payload['short_id']}"
        output.print(f"[ua.ok]resume with:[/ua.ok]  [ua.accent]{cmd}[/ua.accent]")
        output.print(f"[ua.muted]     or:[/ua.muted]  [ua.dim]{alt}[/ua.dim]")
        if launch:
            output.blank()
            output.print(f"[ua.muted]launching TUI resumed to {payload['short_id']}…[/ua.muted]")
            # Launch TUI resumed — delegate to tui_command via runtime
            # We reuse the same store so the resume_count is bumped by the TUI itself
            try:
                store.close()
            except Exception:
                pass
            # Invoke the TUI directly — this replaces the current process flow
            # Use the same parsing as `ua tui --resume <id>` would
            from typer import Context as _Ctx
            # Build a fake context that carries our runtime and the resume flag
            # Easiest: just exec the tui command inline
            import asyncio
            from unknown_agent.tui.shell_app import run_app_shell
            from unknown_agent.config.loader import load_settings
            try:
                settings = load_settings().settings
            except Exception:
                settings = None
            # Re-open store for the TUI (it will be owned by the app)
            from unknown_agent.tui.history import TuiSessionStore as _Store
            _db = runtime.paths.state_dir / "tui_sessions.db"
            _trans = runtime.paths.state_dir / "tui_transcripts"
            _store2 = _Store(_db, _trans)
            # This will block until the TUI exits, then print its own resume message
            asyncio.run(run_app_shell(settings, resume_id=payload["id"], tui_store=_store2))
            raise typer.Exit(0)
    finally:
        try:
            store.close()
        except Exception:
            pass


__all__ = ["app"]
