"""TUI session history — persistence for chat sessions.

Unlike ``memory/sessions.py`` which indexes *runs* (agent executions), this
module stores *conversations* — the back-and-forth inside ``ua tui`` / ``ua shell``.

Each TUI launch is a session: it gets a short ID, a human name, timestamps,
provider/model, and a transcript that survives restarts so the user can
``--resume`` it later.  The store is deliberately self-contained: a single
SQLite file plus one JSONL transcript per session, so deleting the DB is safe
and listing is instant.

Storage layout (under ``AppPaths.state_dir``):
    tui_sessions.db              — index (SQLite)
    tui_transcripts/<id>.jsonl   — transcript, one JSON per line
                                 {role, content, timestamp, extra}

The current session is the one most recently *touched*; ``latest()`` returns it.
``resume`` loads the transcript back into the TUI so the conversation feels
continuous.

Other CLIs do the same with ``--resume <id>`` and a recent-sessions picker —
Unknown Agent now matches that level cleanly.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from unknown_agent.observability.logging import get_logger

log = get_logger("tui.history")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tui_sessions (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    cwd           TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    finished_at   REAL,
    turns         INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_task     TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    transcript_path TEXT NOT NULL DEFAULT '',
    resume_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tui_updated ON tui_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tui_created ON tui_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tui_cwd ON tui_sessions(cwd);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# How long we keep history before auto-prune (0 = never)
DEFAULT_RETAIN = 500


@dataclass(slots=True)
class TuiSessionRecord:
    id: str
    name: str = ""
    title: str = ""
    status: str = "active"  # active | completed | interrupted
    cwd: str = ""
    provider: str = ""
    model: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None
    turns: int = 0
    message_count: int = 0
    last_task: str = ""
    summary: str = ""
    transcript_path: str = ""
    resume_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.title or f"Session {self.id[:8]}"

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def date_str(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.created_at))

    @property
    def time_str(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.created_at))

    @property
    def datetime_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))

    @property
    def updated_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at))

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.created_at)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_id": self.short_id,
            "name": self.name,
            "title": self.title,
            "display_name": self.display_name,
            "status": self.status,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "date": self.date_str,
            "time": self.time_str,
            "datetime": self.datetime_str,
            "turns": self.turns,
            "message_count": self.message_count,
            "last_task": self.last_task,
            "summary": self.summary,
            "transcript_path": self.transcript_path,
            "resume_count": self.resume_count,
            "duration_s": round(self.duration_s, 1),
        }


def _generate_id() -> str:
    # 8-char hex is enough to be unique for 500 sessions, but we use 16 for safety
    # and show 8 as short_id.  Full ID is 16 hex = 8 bytes.
    return uuid.uuid4().hex[:16]


def _generate_name(first_task: str | None, created_at: float | None = None) -> str:
    if first_task:
        cleaned = first_task.strip().splitlines()[0][:60].strip()
        # Remove common prefixes
        for prefix in ("Fix ", "Add ", "Implement ", "Create ", "Build "):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[:60]
                break
        if len(cleaned) >= 4:
            return cleaned[:60]
    ts = created_at or time.time()
    return time.strftime("Session %Y-%m-%d %H:%M", time.localtime(ts))


class TuiSessionStore:
    """SQLite index + JSONL transcripts for TUI chat sessions."""

    def __init__(self, db_path: Path, transcripts_dir: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir = Path(transcripts_dir) if transcripts_dir else self.db_path.parent / "tui_transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "TuiSessionStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transcript helpers -------------------------------------------------
    def _transcript_path_for(self, session_id: str) -> Path:
        return self.transcripts_dir / f"{session_id}.jsonl"

    def _write_transcript(self, session_id: str, messages: list[dict[str, Any]]) -> Path:
        path = self._transcript_path_for(session_id)
        # Write atomically
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        tmp.replace(path)
        return path

    def _append_transcript(self, session_id: str, message: dict[str, Any]) -> Path:
        path = self._transcript_path_for(session_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
        return path

    def load_transcript(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        path = self._transcript_path_for(session_id)
        if not path.exists():
            # Fallback: try legacy DB column if file missing
            row = self.connection.execute("SELECT transcript_path FROM tui_sessions WHERE id=?", (session_id,)).fetchone()
            if row and row["transcript_path"] and Path(row["transcript_path"]).exists():
                path = Path(row["transcript_path"])
            else:
                return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    # -- lifecycle ----------------------------------------------------------
    def create(
        self,
        *,
        cwd: str = "",
        provider: str = "",
        model: str = "",
        name: str | None = None,
        first_task: str | None = None,
        status: str = "active",
    ) -> TuiSessionRecord:
        now = time.time()
        sid = _generate_id()
        session_name = name or _generate_name(first_task, now)
        record = TuiSessionRecord(
            id=sid,
            name=session_name,
            title=session_name,
            status=status,
            cwd=str(Path(cwd).resolve()) if cwd else "",
            provider=provider or "",
            model=model or "",
            created_at=now,
            updated_at=now,
            finished_at=None,
            turns=0,
            message_count=0,
            last_task=first_task[:500] if first_task else "",
            transcript_path=str(self._transcript_path_for(sid)),
        )
        # Seed empty transcript file
        self._transcript_path_for(sid).write_text("", encoding="utf-8")
        self.upsert(record)
        return record

    def upsert(self, record: TuiSessionRecord) -> None:
        now = time.time()
        record.updated_at = record.updated_at or now
        self.connection.execute(
            """
            INSERT INTO tui_sessions(id, name, title, status, cwd, provider, model, created_at, updated_at,
                                     finished_at, turns, message_count, last_task, summary, transcript_path, resume_count)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, title = excluded.title, status = excluded.status, cwd = excluded.cwd,
                provider = excluded.provider, model = excluded.model, updated_at = excluded.updated_at,
                finished_at = excluded.finished_at, turns = excluded.turns, message_count = excluded.message_count,
                last_task = excluded.last_task, summary = excluded.summary, transcript_path = excluded.transcript_path,
                resume_count = excluded.resume_count
            """,
            (
                record.id,
                record.name[:200],
                record.title[:200],
                record.status,
                record.cwd,
                record.provider,
                record.model,
                record.created_at,
                record.updated_at,
                record.finished_at,
                int(record.turns),
                int(record.message_count),
                record.last_task[:1000],
                record.summary[:2000],
                record.transcript_path,
                int(record.resume_count),
            ),
        )
        self.connection.commit()

    def touch(self, session_id: str) -> None:
        self.connection.execute("UPDATE tui_sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
        self.connection.commit()

    def update_on_message(self, session_id: str, role: str, content: str, *, provider: str = "", model: str = "") -> None:
        record = self.get(session_id)
        if record is None:
            return
        message = {"role": role, "content": content, "timestamp": time.time(), "provider": provider, "model": model}
        self._append_transcript(session_id, message)
        record.message_count += 1
        if role == "user":
            record.turns += 1
            record.last_task = content[:500]
            # Auto-name from first user message if generic
            if record.name.startswith("Session ") and content.strip():
                record.name = _generate_name(content, record.created_at)
                record.title = record.name
        record.updated_at = time.time()
        if provider:
            record.provider = provider
        if model:
            record.model = model
        self.upsert(record)

    def finish(self, session_id: str, status: str = "completed") -> TuiSessionRecord | None:
        record = self.get(session_id)
        if record is None:
            return None
        record.status = status
        record.finished_at = time.time()
        record.updated_at = record.finished_at
        self.upsert(record)
        return record

    def delete(self, session_id: str) -> bool:
        # Also delete transcript file
        try:
            self._transcript_path_for(session_id).unlink(missing_ok=True)
        except OSError:
            pass
        cursor = self.connection.execute("DELETE FROM tui_sessions WHERE id = ?", (session_id,))
        self.connection.commit()
        return bool(cursor.rowcount)

    def prune(self, keep: int = DEFAULT_RETAIN) -> int:
        cursor = self.connection.execute(
            "DELETE FROM tui_sessions WHERE id NOT IN (SELECT id FROM tui_sessions ORDER BY updated_at DESC LIMIT ?)",
            (int(keep),),
        )
        self.connection.commit()
        # Also prune orphan transcript files
        try:
            alive = {row["id"] for row in self.connection.execute("SELECT id FROM tui_sessions").fetchall()}
            for p in self.transcripts_dir.glob("*.jsonl"):
                if p.stem not in alive:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except OSError:
            pass
        return int(cursor.rowcount or 0)

    # -- reads --------------------------------------------------------------
    def list(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
        search: str | None = None,
        include_finished: bool = True,
    ) -> list[TuiSessionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if cwd:
            clauses.append("cwd = ?")
            params.append(str(Path(cwd).resolve()))
        if search:
            clauses.append("(name LIKE ? OR title LIKE ? OR last_task LIKE ? OR id LIKE ?)")
            params.extend([f"%{search}%"] * 4)
        if not include_finished:
            clauses.append("status = 'active'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM tui_sessions{where} ORDER BY updated_at DESC LIMIT ?", [*params, limit]
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get(self, session_id: str) -> TuiSessionRecord | None:
        # Exact match first
        row = self.connection.execute("SELECT * FROM tui_sessions WHERE id = ?", (session_id,)).fetchone()
        if row:
            return _row_to_record(row)
        # Then try short-id / substring match (first 8 chars or any substring)
        # This allows `ua tui --resume abc123` to work
        like = f"%{session_id}%"
        row = self.connection.execute("SELECT * FROM tui_sessions WHERE id LIKE ? ORDER BY updated_at DESC LIMIT 1", (like,)).fetchone()
        return _row_to_record(row) if row else None

    def get_by_short(self, short_id: str) -> TuiSessionRecord | None:
        return self.get(short_id)

    def latest(self, *, cwd: str | None = None) -> TuiSessionRecord | None:
        rows = self.list(limit=1, cwd=cwd)
        return rows[0] if rows else None

    def last_active(self, *, cwd: str | None = None) -> TuiSessionRecord | None:
        rows = self.connection.execute(
            "SELECT * FROM tui_sessions WHERE status='active' {} ORDER BY updated_at DESC LIMIT 1".format(
                " AND cwd=?" if cwd else ""
            ),
            ([str(Path(cwd).resolve())] if cwd else []),
        ).fetchall()
        return _row_to_record(rows[0]) if rows else None

    def stats(self) -> dict[str, Any]:
        total = self.connection.execute("SELECT COUNT(*) AS c FROM tui_sessions").fetchone()["c"]
        by_status = {
            row["status"]: row["c"]
            for row in self.connection.execute("SELECT status, COUNT(*) AS c FROM tui_sessions GROUP BY status").fetchall()
        }
        return {
            "sessions": int(total),
            "by_status": by_status,
            "database_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "path": str(self.db_path),
            "transcripts_dir": str(self.transcripts_dir),
        }

    def resume(self, session_id: str) -> TuiSessionRecord | None:
        """Mark a session as resumed — bump resume_count and set status active."""
        record = self.get(session_id)
        if record is None:
            return None
        record.resume_count += 1
        record.status = "active"
        record.updated_at = time.time()
        record.finished_at = None
        self.upsert(record)
        return record

    def set_current_marker(self, session_id: str) -> None:
        """Store the current session ID in a tiny marker file for crash recovery."""
        try:
            marker = self.db_path.parent / "current_tui_session"
            marker.write_text(session_id, encoding="utf-8")
        except OSError:
            pass

    def get_current_marker(self) -> str | None:
        try:
            marker = self.db_path.parent / "current_tui_session"
            if marker.exists():
                val = marker.read_text(encoding="utf-8").strip()
                return val or None
        except OSError:
            pass
        return None


def _row_to_record(row: sqlite3.Row) -> TuiSessionRecord:
    return TuiSessionRecord(
        id=row["id"],
        name=row["name"],
        title=row["title"],
        status=row["status"],
        cwd=row["cwd"],
        provider=row["provider"],
        model=row["model"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        turns=row["turns"],
        message_count=row["message_count"],
        last_task=row["last_task"],
        summary=row["summary"],
        transcript_path=row["transcript_path"],
        resume_count=row["resume_count"],
    )


__all__ = ["TuiSessionStore", "TuiSessionRecord", "SCHEMA_VERSION"]
