"""``ua memory`` - the project's long-term memory, fully inspectable."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.memory.procedures import ProcedureLibrary
from unknown_agent.memory.store import FACT_KINDS, MemoryStore
from unknown_agent.ui.console import output
from unknown_agent.ui.prompts import confirm_destructive
from unknown_agent.ui.renderers import data_table, elapsed, kv_table

app = typer.Typer(name="memory", help="Search, add and curate long-term project memory.", no_args_is_help=True)


def _store(ctx: typer.Context) -> tuple[Runtime, MemoryStore]:
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    store = MemoryStore(
        loaded.paths.project_dir / "memory" / "memory.db",
        markdown_dir=loaded.paths.project_memory_dir if loaded.settings.memory.write_markdown_mirror else None,
        fts_enabled=loaded.settings.memory.fts_enabled,
    )
    return runtime, store


def _library(ctx: typer.Context) -> ProcedureLibrary:
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    return ProcedureLibrary(loaded.paths.project_dir / "procedures", loaded.paths.procedures_dir)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="What to look for."),
    limit: int = typer.Option(10, "--limit", help="Maximum facts."),
    kind: str = typer.Option("", "--kind", help=f"Restrict to a kind ({', '.join(FACT_KINDS)})."),
) -> None:
    """Search remembered facts and conventions."""

    runtime, store = _store(ctx)
    try:
        facts = store.search(query, limit=limit, kind=kind or None)
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json({"facts": facts})
        return
    if not facts:
        output.print("[ua.muted]no matching facts[/ua.muted]")
        return
    output.print(data_table(["id", "kind", "fact", "confidence"], [(fact["id"], fact["kind"], fact["text"][:110], f"{fact['confidence']:.1f}") for fact in facts], title=f"memory ({len(facts)} match(es))"))


@app.command("remember")
def remember(
    ctx: typer.Context,
    text: list[str] = typer.Argument(..., help="The fact to store (quote it)."),
    kind: str = typer.Option("fact", "--kind", help=f"One of: {', '.join(FACT_KINDS)}."),
    tag: list[str] = typer.Option(None, "--tag", help="Tag (repeatable)."),
    pin: bool = typer.Option(False, "--pin", help="Pin so pruning never removes it."),
) -> None:
    """Store a durable project fact.

    Examples:
      ua memory remember "API handlers live in app/api and must return JSONResponse" --kind convention
      ua memory remember "Use uv run pytest, not bare pytest" --kind command --pin
    """
    runtime, store = _store(ctx)
    payload = " ".join(text).strip()
    if not payload:
        store.close()
        raise UsageError("Nothing to remember.")
    if kind not in FACT_KINDS:
        store.close()
        raise UsageError(f"Unknown kind {kind!r}.", hint=f"Valid kinds: {', '.join(FACT_KINDS)}")
    try:
        fact_id = store.remember(payload, kind=kind, tags=list(tag or []), source="cli", pinned=pin)
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json({"id": fact_id, "kind": kind, "text": payload})
    else:
        output.print(f"[ua.ok]remembered[/ua.ok] fact #{fact_id} ({kind})")


@app.command("list")
def list_facts(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit"),
    kind: str = typer.Option("", "--kind"),
) -> None:
    """List remembered facts, newest first."""

    runtime, store = _store(ctx)
    try:
        facts = store.list(limit=limit, kind=kind or None)
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json({"facts": facts})
        return
    if not facts:
        output.print("[ua.muted]memory is empty[/ua.muted]")
        return
    import time

    output.print(
        data_table(
            ["id", "kind", "fact", "when"],
            [(fact["id"], fact["kind"], fact["text"][:100], time.strftime("%Y-%m-%d", time.localtime(fact["created_at"]))) for fact in facts],
            title=f"memory ({len(facts)} facts)",
        )
    )


@app.command("forget")
def forget(
    ctx: typer.Context,
    fact_id: int = typer.Argument(..., help="Fact id to delete."),
) -> None:
    """Delete a fact that turned out to be wrong."""

    runtime, store = _store(ctx)
    try:
        fact = store.get(fact_id)
        if fact is None:
            raise UsageError(f"No fact with id {fact_id}.")
        if not runtime.assume_yes and not confirm_destructive(runtime.prompter, f"forget fact #{fact_id}", preview=fact["text"][:120]):
            output.print("[ua.muted]cancelled[/ua.muted]")
            return
        store.forget(fact_id)
    finally:
        store.close()
    output.print(f"[ua.ok]forgot[/ua.ok] fact #{fact_id}")


@app.command("stats")
def stats(ctx: typer.Context) -> None:
    """Memory size, kinds and retention."""

    runtime, store = _store(ctx)
    try:
        payload = store.stats().to_dict()
    finally:
        store.close()
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    rows = [
        ("facts", payload["facts"]),
        ("by kind", payload["by_kind"] or "none"),
        ("full-text search", "enabled" if payload["fts"] else "unavailable (LIKE fallback)"),
        ("database", f"{payload['database_bytes'] / 1024:.0f} KiB"),
        ("path", payload["path"]),
    ]
    output.print(kv_table(rows, title="memory stats"))


@app.command("export")
def export(
    ctx: typer.Context,
    out: Path = typer.Option(Path("memory-export.json"), "--out", help="Output file."),
) -> None:
    """Export all facts to JSON (backup, sharing, review)."""

    runtime, store = _store(ctx)
    try:
        payload = store.export(out)
    finally:
        store.close()
    output.print(f"[ua.ok]exported[/ua.ok] {len(payload['facts'])} facts to {out}")


@app.command("import")
def import_facts(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="JSON file produced by `ua memory export`."),
) -> None:
    """Import facts from a JSON export (idempotent)."""

    runtime, store = _store(ctx)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        count = store.import_facts(payload)
    finally:
        store.close()
    output.print(f"[ua.ok]imported[/ua.ok] {count} facts")


@app.command("procedures")
def procedures(
    ctx: typer.Context,
    scope: str = typer.Option("", "--scope", help="user | project."),
) -> None:
    """List stored procedures (reusable workflows the agent follows)."""

    runtime = ctx.obj
    library = _library(ctx)
    items = [item for item in library.list(refresh=True) if not scope or item.scope == scope]
    if runtime.is_machine:
        runtime.emit_json({"procedures": [item.to_dict() for item in items]})
        return
    if not items:
        output.print("[ua.muted]no procedures stored[/ua.muted]")
        output.print("[ua.muted]add one with: ua memory procedure-add ship-migration --from-file migration.md[/ua.muted]")
        return
    output.print(
        data_table(
            ["name", "scope", "triggers", "description"],
            [(item.name, item.scope, ", ".join(item.triggers) or "-", item.description[:80]) for item in items],
            title=f"procedures ({len(items)})",
        )
    )


@app.command("procedure-add")
def procedure_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Short kebab-case name."),
    body: str | None = typer.Option(None, "--body", help="Procedure body (markdown)."),
    from_file: Path | None = typer.Option(None, "--from-file", help="Read the body from a file."),
    trigger: list[str] = typer.Option(None, "--trigger", help="Regex that activates this procedure (repeatable)."),
    scope: str = typer.Option("project", "--scope", help="user | project."),
) -> None:
    """Store a reusable workflow the agent should follow when it matches."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    if from_file:
        content = from_file.read_text(encoding="utf-8")
    elif body:
        content = body
    else:
        raise UsageError("Provide --body or --from-file.")
    directory = loaded.paths.procedures_dir if scope == "user" else loaded.paths.project_dir / "procedures"
    library = ProcedureLibrary(directory)
    path = library.save(name, content, scope=scope, triggers=list(trigger or []), directory=directory)
    output.print(f"[ua.ok]saved[/ua.ok] procedure {name!r} to {path}")


@app.command("procedure-remove")
def procedure_remove(ctx: typer.Context, name: str = typer.Argument(..., help="Procedure name.")) -> None:
    """Delete a stored procedure."""

    runtime: Runtime = ctx.obj
    library = _library(ctx)
    if not runtime.assume_yes and not confirm_destructive(runtime.prompter, f"delete procedure {name!r}"):
        output.print("[ua.muted]cancelled[/ua.muted]")
        return
    removed = library.delete(name)
    output.print(f"[ua.ok]removed[/ua.ok] {name}" if removed else "[ua.warn]procedure not found[/ua.warn]")


@app.command("prune")
def prune(
    ctx: typer.Context,
    days: int | None = typer.Option(None, "--days", help="Retention window (defaults to config)."),
) -> None:
    """Delete stale unpinned facts older than the retention window."""

    runtime, store = _store(ctx)
    loaded = runtime.load()
    window = days if days is not None else loaded.settings.memory.retention_days
    try:
        removed = store.prune(window)
    finally:
        store.close()
    output.print(f"[ua.ok]pruned[/ua.ok] {removed} fact(s) older than {window} days")


__all__ = ["app"]
