"""Session and run index.

Every run writes a full journal inside ``.unknown/runs/<id>/`` (``events.jsonl``,
``run.json``, ``plan.json``, ``verification.json``, ``diff.patch``).  This module
maintains the *index* across runs so the CLI can answer "what did I run last week
and what did it change?" without scanning directories, and so a run can be
resumed or audited later.

Deleting this database is always safe: it only mirrors run directories.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.observability.logging import get_logger

log = get_logger("memory.sessions")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    task          TEXT NOT NULL DEFAULT '',
    mode          TEXT NOT NULL DEFAULT 'auto',
    status        TEXT NOT NULL DEFAULT 'pending',
    cwd           TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    step          INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    finished_at   REAL,
    summary       TEXT NOT NULL DEFAULT '',
    blockers      TEXT NOT NULL DEFAULT '',
    run_path      TEXT NOT NULL DEFAULT '',
    files_changed INTEGER NOT NULL DEFAULT 0,
    gates_passed  INTEGER NOT NULL DEFAULT 0,
    gates_failed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass(slots=True)
class SessionRecord:
    id: str
    task: str = ""
    mode: str = "auto"
    status: str = "pending"
    cwd: str = ""
    provider: str = ""
    model: str = ""
    step: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None
    summary: str = ""
    blockers: str = ""
    run_path: str = ""
    files_changed: int = 0
    gates_passed: int = 0
    gates_failed: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return (self.finished_at or time.time()) - self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "mode": self.mode,
            "status": self.status,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "step": self.step,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "summary": self.summary,
            "blockers": [item for item in self.blockers.split("\n") if item],
            "run_path": self.run_path,
            "files_changed": self.files_changed,
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
        }


def _as_count(value: Any) -> int:
    """``files_changed`` is stored as a count; a list is accepted for convenience."""

    if isinstance(value, (list, tuple, set)):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class SessionStore:
    """SQLite index of runs for one machine (all projects, keyed by cwd)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:  # pragma: no cover - already closed
            pass

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def upsert(self, record: SessionRecord) -> None:
        now = time.time()
        self.connection.execute(
            """
            INSERT INTO sessions(id, task, mode, status, cwd, provider, model, step, created_at, updated_at,
                                 finished_at, summary, blockers, run_path, files_changed, gates_passed, gates_failed)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                task = excluded.task, mode = excluded.mode, status = excluded.status, cwd = excluded.cwd,
                provider = excluded.provider, model = excluded.model, step = excluded.step,
                updated_at = excluded.updated_at, finished_at = excluded.finished_at, summary = excluded.summary,
                blockers = excluded.blockers, run_path = excluded.run_path, files_changed = excluded.files_changed,
                gates_passed = excluded.gates_passed, gates_failed = excluded.gates_failed
            """,
            (
                record.id,
                record.task[:2000],
                record.mode,
                record.status,
                record.cwd,
                record.provider,
                record.model,
                int(record.step),
                record.created_at or now,
                now,
                record.finished_at,
                record.summary[:4000],
                record.blockers,
                record.run_path,
                _as_count(record.files_changed),
                int(record.gates_passed),
                int(record.gates_failed),
            ),
        )
        self.connection.commit()

    def record_from_run_state(self, state: Any, *, run_path: Path | None = None) -> SessionRecord:
        """Index a finished :class:`~unknown_agent.core.models.RunState`."""

        verification = getattr(state, "verification", None)
        results = list(verification.results) if verification is not None else []
        passed = len([item for item in results if item.status.value == "passed"])
        failed = len([item for item in results if item.status.value in {"failed", "error"}])
        record = SessionRecord(
            id=state.id,
            task=state.task,
            mode=state.mode,
            status=state.status.value,
            cwd=state.cwd,
            provider=state.provider,
            model=state.model,
            step=state.step,
            created_at=state.started_at,
            updated_at=time.time(),
            finished_at=state.finished_at,
            summary=state.summary,
            blockers="\n".join(state.blockers),
            run_path=str(run_path) if run_path else "",
            files_changed=len(state.files_changed),
            gates_passed=passed,
            gates_failed=failed,
        )
        self.upsert(record)
        return record

    def delete(self, session_id: str) -> bool:
        cursor = self.connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.connection.commit()
        return bool(cursor.rowcount)

    def prune(self, keep: int = 200) -> int:
        cursor = self.connection.execute(
            "DELETE FROM sessions WHERE id NOT IN (SELECT id FROM sessions ORDER BY created_at DESC LIMIT ?)",
            (int(keep),),
        )
        self.connection.commit()
        return int(cursor.rowcount or 0)

    # -- reads -------------------------------------------------------------
    def list(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        cwd: str | None = None,
        search: str | None = None,
    ) -> list[SessionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if cwd:
            clauses.append("cwd = ?")
            params.append(str(Path(cwd).resolve()))
        if search:
            clauses.append("(task LIKE ? OR summary LIKE ? OR id LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM sessions{where} ORDER BY created_at DESC LIMIT ?", [*params, limit]
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get(self, session_id: str) -> SessionRecord | None:
        row = self.connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _row_to_record(row) if row else None

    def last(self, *, cwd: str | None = None) -> SessionRecord | None:
        rows = self.list(limit=1, cwd=cwd)
        return rows[0] if rows else None

    def stats(self) -> dict[str, Any]:
        total = self.connection.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
        by_status = {
            row["status"]: row["c"]
            for row in self.connection.execute("SELECT status, COUNT(*) AS c FROM sessions GROUP BY status").fetchall()
        }
        return {
            "sessions": int(total),
            "by_status": by_status,
            "database_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "path": str(self.db_path),
        }

    def export_json(self, path: Path) -> int:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": time.time(),
            "sessions": [record.to_dict() for record in self.list(limit=10_000)],
        }
        Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(payload["sessions"])


def _row_to_record(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        task=row["task"],
        mode=row["mode"],
        status=row["status"],
        cwd=row["cwd"],
        provider=row["provider"],
        model=row["model"],
        step=row["step"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        summary=row["summary"],
        blockers=row["blockers"],
        run_path=row["run_path"],
        files_changed=row["files_changed"],
        gates_passed=row["gates_passed"],
        gates_failed=row["gates_failed"],
    )


__all__ = ["SessionStore", "SessionRecord", "SCHEMA_VERSION"]
