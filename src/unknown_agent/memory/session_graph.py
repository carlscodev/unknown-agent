"""Event-sourced session branches, forks and reproducible state.

A branch stores only events created after its fork. Ancestor history is shared,
not copied, so a 200k-token session can explore three alternatives without
tripling storage. Appends use optimistic head checks, each event participates in
a SHA-256 chain, and merges detect semantic conflicts in values and artifacts
before replaying source events with provenance.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BranchConflict",
    "ConcurrentAppend",
    "SessionEvent",
    "SessionBranch",
    "MergePreview",
    "SessionGraphStore",
]


class BranchConflict(RuntimeError):
    """A merge would overwrite divergent semantic state."""


class ConcurrentAppend(RuntimeError):
    """The caller appended against a stale branch head."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_branches (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    fork_event_id INTEGER,
    head_event_id INTEGER,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(session_id, name),
    FOREIGN KEY(parent_id) REFERENCES session_branches(id)
);
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    parent_event_id INTEGER,
    previous_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(branch_id, seq),
    FOREIGN KEY(branch_id) REFERENCES session_branches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_session_events_branch ON session_events(branch_id, seq);
CREATE INDEX IF NOT EXISTS idx_session_branches_session ON session_branches(session_id, created_at);
CREATE TABLE IF NOT EXISTS session_snapshots (
    branch_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(branch_id, event_id)
);
"""


@dataclass(frozen=True, slots=True)
class SessionEvent:
    id: int
    session_id: str
    branch_id: str
    seq: int
    kind: str
    role: str
    payload: dict[str, Any]
    parent_event_id: int | None
    previous_hash: str
    content_hash: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "branch_id": self.branch_id,
            "seq": self.seq,
            "kind": self.kind,
            "role": self.role,
            "payload": self.payload,
            "parent_event_id": self.parent_event_id,
            "previous_hash": self.previous_hash,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class SessionBranch:
    id: str
    session_id: str
    name: str
    parent_id: str | None = None
    fork_event_id: int | None = None
    head_event_id: int | None = None
    created_at: float = 0.0
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "fork_event_id": self.fork_event_id,
            "head_event_id": self.head_event_id,
            "created_at": self.created_at,
            "status": self.status,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class MergePreview:
    source: str
    target: str
    common_event_id: int | None
    events_to_replay: list[SessionEvent] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "common_event_id": self.common_event_id,
            "clean": self.clean,
            "conflicts": self.conflicts,
            "events": [event.to_dict() for event in self.events_to_replay],
        }


class SessionGraphStore:
    """Durable branch graph for agent transcripts and state mutations."""

    def __init__(self, db_path: Path | str, *, snapshot_interval: int = 50) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_interval = max(1, snapshot_interval)
        self.connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(_SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SessionGraphStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_session(
        self, session_id: str, *, branch_name: str = "main", metadata: dict[str, Any] | None = None
    ) -> SessionBranch:
        branch = SessionBranch(
            id=f"b-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            name=branch_name,
            created_at=time.time(),
            metadata=dict(metadata or {}),
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO session_branches(id,session_id,name,parent_id,fork_event_id,head_event_id,"
                "created_at,status,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    branch.id,
                    branch.session_id,
                    branch.name,
                    None,
                    None,
                    None,
                    branch.created_at,
                    branch.status,
                    _canonical(branch.metadata),
                ),
            )
        return branch

    def branch(self, identifier: str, *, session_id: str = "") -> SessionBranch | None:
        if session_id:
            row = self.connection.execute(
                "SELECT * FROM session_branches WHERE session_id=? AND (id=? OR name=?)",
                (session_id, identifier, identifier),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM session_branches WHERE id=?", (identifier,)
            ).fetchone()
        return _branch(row) if row else None

    def branches(self, session_id: str) -> list[SessionBranch]:
        rows = self.connection.execute(
            "SELECT * FROM session_branches WHERE session_id=? ORDER BY created_at", (session_id,)
        ).fetchall()
        return [_branch(row) for row in rows]

    def append(
        self,
        branch_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        role: str = "",
        expected_head: int | None = None,
    ) -> SessionEvent:
        """Atomically append, rejecting callers that read a stale head."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            branch_row = self.connection.execute(
                "SELECT * FROM session_branches WHERE id=?", (branch_id,)
            ).fetchone()
            if branch_row is None:
                raise KeyError(branch_id)
            actual_head = branch_row["head_event_id"]
            if expected_head is not None and actual_head != expected_head:
                raise ConcurrentAppend(
                    f"branch head moved from expected {expected_head} to {actual_head}; reload before appending"
                )
            previous_hash = self._anchor_hash(branch_row)
            seq_row = self.connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS n FROM session_events WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
            seq = int(seq_row["n"])
            body = _canonical(payload)
            content_hash = _event_hash(previous_hash, kind, role, body)
            now = time.time()
            cursor = self.connection.execute(
                "INSERT INTO session_events(session_id,branch_id,seq,kind,role,payload,parent_event_id,"
                "previous_hash,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    branch_row["session_id"],
                    branch_id,
                    seq,
                    kind,
                    role,
                    body,
                    actual_head or branch_row["fork_event_id"],
                    previous_hash,
                    content_hash,
                    now,
                ),
            )
            event_id = int(cursor.lastrowid or 0)
            self.connection.execute(
                "UPDATE session_branches SET head_event_id=? WHERE id=?", (event_id, branch_id)
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        event = self.event(event_id)
        if event is None:  # pragma: no cover - same transaction inserted it
            raise RuntimeError("appended event disappeared")
        if seq % self.snapshot_interval == 0:
            self.snapshot(branch_id)
        return event

    def event(self, event_id: int) -> SessionEvent | None:
        row = self.connection.execute(
            "SELECT * FROM session_events WHERE id=?", (event_id,)
        ).fetchone()
        return _event(row) if row else None

    def fork(
        self,
        parent_id: str,
        name: str,
        *,
        at_event_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionBranch:
        parent = self.branch(parent_id)
        if parent is None:
            raise KeyError(parent_id)
        fork_event = at_event_id if at_event_id is not None else parent.head_event_id
        if fork_event is not None and fork_event not in {event.id for event in self.timeline(parent.id)}:
            raise ValueError(f"event {fork_event} is not visible from branch {parent.name}")
        child = SessionBranch(
            id=f"b-{uuid.uuid4().hex[:12]}",
            session_id=parent.session_id,
            name=name,
            parent_id=parent.id,
            fork_event_id=fork_event,
            created_at=time.time(),
            metadata=dict(metadata or {}),
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO session_branches(id,session_id,name,parent_id,fork_event_id,head_event_id,"
                "created_at,status,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    child.id,
                    child.session_id,
                    child.name,
                    child.parent_id,
                    child.fork_event_id,
                    None,
                    child.created_at,
                    child.status,
                    _canonical(child.metadata),
                ),
            )
        return child

    def own_events(self, branch_id: str) -> list[SessionEvent]:
        rows = self.connection.execute(
            "SELECT * FROM session_events WHERE branch_id=? ORDER BY seq", (branch_id,)
        ).fetchall()
        return [_event(row) for row in rows]

    def timeline(self, branch_id: str, *, until_event_id: int | None = None) -> list[SessionEvent]:
        branch = self.branch(branch_id)
        if branch is None:
            raise KeyError(branch_id)
        events: list[SessionEvent] = []
        if branch.parent_id:
            events.extend(self.timeline(branch.parent_id, until_event_id=branch.fork_event_id))
        for event in self.own_events(branch.id):
            events.append(event)
            if until_event_id is not None and event.id == until_event_id:
                break
        if until_event_id is not None:
            for index, event in enumerate(events):
                if event.id == until_event_id:
                    return events[: index + 1]
        return events

    def materialize(self, branch_id: str) -> dict[str, Any]:
        state: dict[str, Any] = {
            "messages": [],
            "tools": [],
            "artifacts": {},
            "values": {},
            "checkpoints": [],
            "status": "active",
        }
        for event in self.timeline(branch_id):
            _reduce(state, event)
        state["branch_id"] = branch_id
        state["event_count"] = len(self.timeline(branch_id))
        return state

    def snapshot(self, branch_id: str) -> dict[str, Any]:
        branch = self.branch(branch_id)
        if branch is None:
            raise KeyError(branch_id)
        state = self.materialize(branch_id)
        body = _canonical(state)
        digest = hashlib.sha256(body.encode()).hexdigest()
        event_id = branch.head_event_id or branch.fork_event_id or 0
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO session_snapshots(branch_id,event_id,state,state_hash,created_at) "
                "VALUES(?,?,?,?,?)",
                (branch_id, event_id, body, digest, time.time()),
            )
        return {"branch_id": branch_id, "event_id": event_id, "state_hash": digest, "state": state}

    def verify(self, branch_id: str) -> list[str]:
        """Return integrity errors for this branch's own hash chain."""

        branch = self.branch(branch_id)
        if branch is None:
            raise KeyError(branch_id)
        errors: list[str] = []
        if branch.fork_event_id:
            anchor = self.event(branch.fork_event_id)
            previous = anchor.content_hash if anchor else ""
        else:
            previous = ""
        for event in self.own_events(branch_id):
            body = _canonical(event.payload)
            expected = _event_hash(previous, event.kind, event.role, body)
            if event.previous_hash != previous:
                errors.append(f"event {event.id}: previous hash mismatch")
            if event.content_hash != expected:
                errors.append(f"event {event.id}: content hash mismatch")
            previous = event.content_hash
        return errors

    def preview_merge(self, source_id: str, target_id: str) -> MergePreview:
        source = self.branch(source_id)
        target = self.branch(target_id)
        if source is None or target is None:
            raise KeyError(source_id if source is None else target_id)
        if source.session_id != target.session_id:
            raise ValueError("cannot merge branches from different sessions")
        if source.parent_id != target.id:
            raise ValueError("source must be a direct fork of target; rebase nested forks first")
        source_events = self.own_events(source.id)
        target_after_fork = self._events_after(target.id, source.fork_event_id)
        source_writes = _semantic_writes(source_events)
        target_writes = _semantic_writes(target_after_fork)
        conflicts = sorted(
            key
            for key in source_writes.keys() & target_writes.keys()
            if source_writes[key] != target_writes[key]
        )
        return MergePreview(
            source=source.id,
            target=target.id,
            common_event_id=source.fork_event_id,
            events_to_replay=source_events,
            conflicts=conflicts,
        )

    def merge(self, source_id: str, target_id: str) -> list[SessionEvent]:
        preview = self.preview_merge(source_id, target_id)
        if not preview.clean:
            raise BranchConflict("semantic conflicts: " + ", ".join(preview.conflicts))
        target = self.branch(target_id)
        if target is None:  # pragma: no cover - preview checked it
            raise KeyError(target_id)
        appended: list[SessionEvent] = []
        expected = target.head_event_id
        for source_event in preview.events_to_replay:
            payload = dict(source_event.payload)
            payload["_merge_provenance"] = {
                "source_branch": source_id,
                "source_event": source_event.id,
                "source_hash": source_event.content_hash,
            }
            event = self.append(
                target_id,
                source_event.kind,
                payload,
                role=source_event.role,
                expected_head=expected,
            )
            expected = event.id
            appended.append(event)
        with self.connection:
            self.connection.execute(
                "UPDATE session_branches SET status='merged' WHERE id=?", (source_id,)
            )
        return appended

    def _events_after(self, branch_id: str, event_id: int | None) -> list[SessionEvent]:
        events = self.timeline(branch_id)
        if event_id is None:
            return events
        for index, event in enumerate(events):
            if event.id == event_id:
                return events[index + 1 :]
        return events

    def _anchor_hash(self, branch_row: sqlite3.Row) -> str:
        head = branch_row["head_event_id"]
        anchor = head or branch_row["fork_event_id"]
        if not anchor:
            return ""
        row = self.connection.execute(
            "SELECT content_hash FROM session_events WHERE id=?", (anchor,)
        ).fetchone()
        return str(row["content_hash"]) if row else ""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _event_hash(previous: str, kind: str, role: str, payload: str) -> str:
    return hashlib.sha256(f"{previous}\0{kind}\0{role}\0{payload}".encode()).hexdigest()


def _branch(row: sqlite3.Row) -> SessionBranch:
    return SessionBranch(
        id=row["id"],
        session_id=row["session_id"],
        name=row["name"],
        parent_id=row["parent_id"],
        fork_event_id=row["fork_event_id"],
        head_event_id=row["head_event_id"],
        created_at=row["created_at"],
        status=row["status"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


def _event(row: sqlite3.Row) -> SessionEvent:
    return SessionEvent(
        id=row["id"],
        session_id=row["session_id"],
        branch_id=row["branch_id"],
        seq=row["seq"],
        kind=row["kind"],
        role=row["role"],
        payload=json.loads(row["payload"]),
        parent_event_id=row["parent_event_id"],
        previous_hash=row["previous_hash"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
    )


def _reduce(state: dict[str, Any], event: SessionEvent) -> None:
    payload = event.payload
    if event.kind == "message.added":
        state["messages"].append({"role": event.role, **payload, "event_id": event.id})
    elif event.kind.startswith("tool."):
        state["tools"].append({"kind": event.kind, **payload, "event_id": event.id})
    elif event.kind == "artifact.updated":
        state["artifacts"][str(payload.get("path", ""))] = payload
    elif event.kind == "value.set":
        state["values"][str(payload.get("key", ""))] = payload.get("value")
    elif event.kind == "checkpoint":
        state["checkpoints"].append({**payload, "event_id": event.id})
    elif event.kind == "session.status":
        state["status"] = str(payload.get("status", state["status"]))


def _semantic_writes(events: list[SessionEvent]) -> dict[str, str]:
    writes: dict[str, str] = {}
    for event in events:
        key = ""
        value: Any = None
        if event.kind == "value.set":
            key = f"value:{event.payload.get('key', '')}"
            value = event.payload.get("value")
        elif event.kind == "artifact.updated":
            key = f"artifact:{event.payload.get('path', '')}"
            value = event.payload.get("sha256") or event.payload.get("content")
        if key:
            writes[key] = _canonical(value)
    return writes
