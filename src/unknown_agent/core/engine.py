"""Long-horizon mission engine: missions that outlive the process running them.

An agent that can work for ten minutes is a demo.  An agent that can work for
three weeks across reboots, network partitions and ``SIGTERM`` is a tool.  The
difference is entirely in the bookkeeping, and this module is that bookkeeping.

The design rests on four commitments:

**Every state change is a committed transaction.**  A mission is a DAG of
:class:`MissionNode` rows in SQLite running in WAL mode.  Nothing lives only in
Python memory, so ``kill -9`` loses at most the in-flight tool call — never the
plan, the ledger, or which node had already been consolidated.

**Resumption verifies reality instead of trusting the record.**  On
:meth:`LongHorizonEngine.resume_mission` the engine re-hashes every file it
previously touched.  A node whose outputs are still on disk is *consolidated*
and skipped; a node whose file has drifted is reopened with the drift named.
This is what stops a resumed mission from redoing three days of work, and
equally from building on top of changes someone reverted while it was down.

**A crash is a state, not an absence.**  A node found in ``RUNNING`` at startup
did not finish — the process died inside it.  It is reopened with its attempt
count intact, so a node that has crashed the runtime twice is not retried a
third time forever.

**Going in circles is a failure mode the engine detects itself.**  After each
attempt a node records an *observation fingerprint*: the hash of what actually
changed in the world (files touched, tests passing, gates green).  When two
attempts produce byte-identical observations, the work is not progressing.
:class:`EntropyDetector` freezes the subtask and asks for a causal re-plan
rather than letting a worker burn a month of tokens re-reading the same file.

The engine is executor-agnostic: supply anything satisfying :class:`NodeExecutor`
and the persistence, recovery, heartbeat and entropy machinery apply unchanged,
which is also what makes it testable without a live model.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from unknown_agent.observability.logging import get_logger

log = get_logger("core.engine")

__all__ = [
    "MissionStatus",
    "NodeStatus",
    "MissionNode",
    "Mission",
    "NodeOutcome",
    "NodeExecutor",
    "FileFingerprint",
    "DriftReport",
    "ResumeReport",
    "EntropyDetector",
    "EntropyVerdict",
    "MissionStore",
    "LongHorizonEngine",
    "EngineError",
]

SCHEMA_VERSION = 1
#: Missing this many seconds of heartbeat means the owning process is gone.
DEFAULT_HEARTBEAT_INTERVAL = 10.0
STALE_HEARTBEAT_FACTOR = 6.0


class EngineError(RuntimeError):
    """The engine refused to do something that would corrupt mission state."""


# --------------------------------------------------------------------------- #
# states
# --------------------------------------------------------------------------- #
class MissionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"

    @property
    def terminal(self) -> bool:
        return self in {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.ABANDONED}


class NodeStatus(str, Enum):
    #: Dependencies not yet satisfied.
    BLOCKED = "blocked"
    #: Ready to run.
    READY = "ready"
    #: A worker holds it right now.
    RUNNING = "running"
    #: Finished and its effects verified present on disk.
    CONSOLIDATED = "consolidated"
    #: Finished but effects not yet verified (crash window).
    DONE_UNVERIFIED = "done_unverified"
    FAILED = "failed"
    #: Entropy detector stopped it; needs a re-plan before it runs again.
    FROZEN = "frozen"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in {NodeStatus.CONSOLIDATED, NodeStatus.SKIPPED}

    @property
    def finished(self) -> bool:
        return self in {
            NodeStatus.CONSOLIDATED,
            NodeStatus.DONE_UNVERIFIED,
            NodeStatus.SKIPPED,
        }


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class MissionNode:
    """One unit of work in the mission DAG."""

    key: str
    title: str
    role: str = "coder"
    status: NodeStatus = NodeStatus.BLOCKED
    depends_on: tuple[str, ...] = ()
    attempts: int = 0
    max_attempts: int = 3
    #: Files this node is expected to produce or modify.
    outputs: tuple[str, ...] = ()
    model: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    note: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    #: Hash of the last observed world-state after an attempt.
    observation: str = ""
    complexity: int = 3

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts

    @property
    def duration_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "role": self.role,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "outputs": list(self.outputs),
            "model": self.model,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            "note": self.note,
            "result": self.result,
            "observation": self.observation,
            "complexity": self.complexity,
        }


@dataclass(slots=True)
class Mission:
    """A long-running goal and its DAG."""

    id: str
    goal: str
    cwd: str
    status: MissionStatus = MissionStatus.PENDING
    created_at: float = 0.0
    updated_at: float = 0.0
    nodes: dict[str, MissionNode] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def progress(self) -> tuple[int, int]:
        done = sum(1 for node in self.nodes.values() if node.status.terminal)
        return done, len(self.nodes)

    @property
    def uptime_s(self) -> float:
        return max(0.0, time.time() - self.created_at) if self.created_at else 0.0

    def ready_nodes(self) -> list[MissionNode]:
        """Nodes whose dependencies are all consolidated."""

        out: list[MissionNode] = []
        for node in self.nodes.values():
            if node.status not in {NodeStatus.READY, NodeStatus.BLOCKED}:
                continue
            deps_ok = all(
                self.nodes[dep].status.terminal for dep in node.depends_on if dep in self.nodes
            )
            if deps_ok:
                out.append(node)
        return sorted(out, key=lambda item: item.key)

    def to_dict(self) -> dict[str, Any]:
        done, total = self.progress
        return {
            "id": self.id,
            "goal": self.goal,
            "cwd": self.cwd,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "uptime_s": round(self.uptime_s, 1),
            "progress": {"done": done, "total": total},
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "meta": self.meta,
        }


@dataclass(slots=True)
class NodeOutcome:
    """What an executor reports back about one attempt."""

    ok: bool
    summary: str = ""
    error: str = ""
    #: Files the attempt actually wrote, for drift verification on resume.
    touched_files: tuple[str, ...] = ()
    #: Signals the entropy detector hashes: gate results, test counts, etc.
    signals: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "error": self.error,
            "touched_files": list(self.touched_files),
            "signals": self.signals,
            "data": self.data,
        }


class NodeExecutor(Protocol):
    """Whatever actually does the work for one node."""

    async def execute(self, node: MissionNode, mission: Mission) -> NodeOutcome:
        ...


# --------------------------------------------------------------------------- #
# drift detection
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FileFingerprint:
    path: str
    sha256: str
    size: int
    recorded_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "recorded_at": self.recorded_at,
        }


@dataclass(slots=True)
class DriftReport:
    """What changed on disk while the engine was not looking."""

    unchanged: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.modified and not self.deleted

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "unchanged": list(self.unchanged),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
        }


@dataclass(slots=True)
class ResumeReport:
    """The full account of what resuming a mission decided."""

    mission_id: str
    resumed: bool
    skipped_consolidated: list[str] = field(default_factory=list)
    reopened_crashed: list[str] = field(default_factory=list)
    reopened_drifted: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    drift: DriftReport = field(default_factory=DriftReport)
    previous_owner_pid: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "resumed": self.resumed,
            "skipped_consolidated": list(self.skipped_consolidated),
            "reopened_crashed": list(self.reopened_crashed),
            "reopened_drifted": list(self.reopened_drifted),
            "frozen": list(self.frozen),
            "drift": self.drift.to_dict(),
            "previous_owner_pid": self.previous_owner_pid,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        parts = [f"resumed {self.mission_id}"]
        if self.skipped_consolidated:
            parts.append(f"{len(self.skipped_consolidated)} already consolidated (no tokens spent)")
        if self.reopened_crashed:
            parts.append(f"{len(self.reopened_crashed)} reopened after crash")
        if self.reopened_drifted:
            parts.append(f"{len(self.reopened_drifted)} reopened due to disk drift")
        if self.frozen:
            parts.append(f"{len(self.frozen)} frozen pending re-plan")
        return "; ".join(parts)


def hash_file(path: Path) -> str:
    """SHA-256 of a file, streamed so a 500 MB asset does not blow memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131_072), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# entropy
# --------------------------------------------------------------------------- #
class EntropyVerdict(str, Enum):
    PROGRESSING = "progressing"
    #: Same observation twice — warn but allow one more attempt.
    REPEATING = "repeating"
    #: No movement after the threshold. Freeze and re-plan.
    STALLED = "stalled"


class EntropyDetector:
    """Notices when a worker is busy without getting anywhere.

    "Busy" is cheap to fake: re-reading a file, re-running a passing test, or
    rewriting the same broken patch all look like activity.  The only honest
    measure of progress is whether the *world* changed, so this hashes the
    observable outcome of each attempt — files touched plus whatever signals
    the executor reports — and compares consecutive attempts.
    """

    def __init__(self, *, threshold: int = 2) -> None:
        # Two identical observations is coincidence; the third is a loop. The
        # brief asks for action "more than twice", so the default threshold
        # fires on the third matching attempt.
        self._threshold = max(1, threshold)
        self._history: dict[str, list[str]] = {}

    @staticmethod
    def fingerprint(outcome: NodeOutcome) -> str:
        payload = json.dumps(
            {
                "ok": outcome.ok,
                "files": sorted(outcome.touched_files),
                "signals": outcome.signals,
                "error": (outcome.error or "")[:400],
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    def observe(self, node_key: str, outcome: NodeOutcome) -> tuple[EntropyVerdict, str]:
        mark = self.fingerprint(outcome)
        seen = self._history.setdefault(node_key, [])
        seen.append(mark)
        repeats = sum(1 for item in seen if item == mark)

        if repeats == 1:
            return EntropyVerdict.PROGRESSING, mark
        if repeats <= self._threshold:
            return EntropyVerdict.REPEATING, mark
        return EntropyVerdict.STALLED, mark

    def reset(self, node_key: str) -> None:
        self._history.pop(node_key, None)

    def history(self, node_key: str) -> list[str]:
        return list(self._history.get(node_key, ()))


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS missions (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    cwd         TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS mission_nodes (
    mission_id   TEXT NOT NULL,
    key          TEXT NOT NULL,
    title        TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'coder',
    status       TEXT NOT NULL,
    depends_on   TEXT NOT NULL DEFAULT '[]',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    outputs      TEXT NOT NULL DEFAULT '[]',
    model        TEXT NOT NULL DEFAULT '',
    started_at   REAL NOT NULL DEFAULT 0,
    finished_at  REAL NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    result       TEXT NOT NULL DEFAULT '{}',
    observation  TEXT NOT NULL DEFAULT '',
    complexity   INTEGER NOT NULL DEFAULT 3,
    PRIMARY KEY (mission_id, key),
    FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS mission_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    node_key   TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_fingerprints (
    mission_id  TEXT NOT NULL,
    node_key    TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (mission_id, path)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    mission_id TEXT PRIMARY KEY,
    pid        INTEGER NOT NULL,
    host       TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    phase      TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    label      TEXT NOT NULL,
    ts         REAL NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_mission ON mission_events(mission_id, id);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON mission_nodes(mission_id, status);
"""


class MissionStore:
    """SQLite persistence in WAL mode — the mission's source of truth.

    WAL matters for a specific reason: the web dashboard and the TUI read
    mission state while a worker writes it.  Without WAL those readers would
    block the writer, and a dashboard refresh could stall the agent.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        cursor = self._conn.cursor()
        # WAL lets readers and the writer coexist; NORMAL sync is the standard
        # durability/throughput trade for WAL and survives process death (only
        # an OS-level crash can lose the last commits).
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO engine_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()

    def __enter__(self) -> "MissionStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- missions ----------------------------------------------------------
    def create_mission(self, mission: Mission) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO missions(id, goal, cwd, status, created_at, updated_at, meta) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    mission.id,
                    mission.goal,
                    mission.cwd,
                    mission.status.value,
                    mission.created_at,
                    mission.updated_at,
                    json.dumps(mission.meta, default=str),
                ),
            )
            for node in mission.nodes.values():
                self._insert_node(mission.id, node)

    def _insert_node(self, mission_id: str, node: MissionNode) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO mission_nodes(mission_id, key, title, role, status, depends_on,"
            " attempts, max_attempts, outputs, model, started_at, finished_at, error, note, result,"
            " observation, complexity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mission_id,
                node.key,
                node.title,
                node.role,
                node.status.value,
                json.dumps(list(node.depends_on)),
                node.attempts,
                node.max_attempts,
                json.dumps(list(node.outputs)),
                node.model,
                node.started_at,
                node.finished_at,
                node.error,
                node.note,
                json.dumps(node.result, default=str),
                node.observation,
                node.complexity,
            ),
        )

    def save_node(self, mission_id: str, node: MissionNode) -> None:
        """Persist one node. Atomic — a crash mid-write leaves the old row."""

        with self._conn:
            self._insert_node(mission_id, node)
            self._conn.execute(
                "UPDATE missions SET updated_at = ? WHERE id = ?", (time.time(), mission_id)
            )

    def set_mission_status(self, mission_id: str, status: MissionStatus) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE missions SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, time.time(), mission_id),
            )

    def load_mission(self, mission_id: str) -> Mission | None:
        row = self._conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        if row is None:
            return None
        mission = Mission(
            id=row["id"],
            goal=row["goal"],
            cwd=row["cwd"],
            status=MissionStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            meta=json.loads(row["meta"] or "{}"),
        )
        nodes = self._conn.execute(
            "SELECT * FROM mission_nodes WHERE mission_id = ? ORDER BY key", (mission_id,)
        ).fetchall()
        for node_row in nodes:
            node = MissionNode(
                key=node_row["key"],
                title=node_row["title"],
                role=node_row["role"],
                status=NodeStatus(node_row["status"]),
                depends_on=tuple(json.loads(node_row["depends_on"] or "[]")),
                attempts=node_row["attempts"],
                max_attempts=node_row["max_attempts"],
                outputs=tuple(json.loads(node_row["outputs"] or "[]")),
                model=node_row["model"],
                started_at=node_row["started_at"],
                finished_at=node_row["finished_at"],
                error=node_row["error"],
                note=node_row["note"],
                result=json.loads(node_row["result"] or "{}"),
                observation=node_row["observation"],
                complexity=node_row["complexity"],
            )
            mission.nodes[node.key] = node
        return mission

    def list_missions(self, *, limit: int = 50, status: MissionStatus | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, goal, status, created_at, updated_at FROM missions"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.value)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # -- events ------------------------------------------------------------
    def record_event(
        self, mission_id: str, kind: str, *, node_key: str = "", **payload: Any
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO mission_events(mission_id, node_key, kind, payload, ts) VALUES(?,?,?,?,?)",
                (mission_id, node_key, kind, json.dumps(payload, default=str), time.time()),
            )

    def events(
        self, mission_id: str, *, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, node_key, kind, payload, ts FROM mission_events "
            "WHERE mission_id = ? AND id > ? ORDER BY id LIMIT ?",
            (mission_id, after_id, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"] or "{}")
            out.append(item)
        return out

    # -- fingerprints ------------------------------------------------------
    def record_fingerprint(self, mission_id: str, node_key: str, fingerprint: FileFingerprint) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO file_fingerprints"
                "(mission_id, node_key, path, sha256, size, recorded_at) VALUES(?,?,?,?,?,?)",
                (
                    mission_id,
                    node_key,
                    fingerprint.path,
                    fingerprint.sha256,
                    fingerprint.size,
                    fingerprint.recorded_at,
                ),
            )

    def fingerprints(self, mission_id: str, *, node_key: str = "") -> list[FileFingerprint]:
        sql = "SELECT path, sha256, size, recorded_at FROM file_fingerprints WHERE mission_id = ?"
        params: list[Any] = [mission_id]
        if node_key:
            sql += " AND node_key = ?"
            params.append(node_key)
        return [
            FileFingerprint(row["path"], row["sha256"], row["size"], row["recorded_at"])
            for row in self._conn.execute(sql, params).fetchall()
        ]

    # -- heartbeat ---------------------------------------------------------
    def beat(self, mission_id: str, *, pid: int, phase: str = "", note: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO heartbeats(mission_id, pid, host, ts, phase, note) "
                "VALUES(?,?,?,?,?,?)",
                (mission_id, pid, os.uname().nodename if hasattr(os, "uname") else "", time.time(), phase, note),
            )

    def last_beat(self, mission_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT pid, host, ts, phase, note FROM heartbeats WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- checkpoints -------------------------------------------------------
    def checkpoint(self, mission_id: str, label: str, payload: Mapping[str, Any]) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO checkpoints(mission_id, label, ts, payload) VALUES(?,?,?,?)",
                (mission_id, label, time.time(), json.dumps(dict(payload), default=str)),
            )
            return int(cursor.lastrowid or 0)

    def checkpoints(self, mission_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, label, ts, payload FROM checkpoints WHERE mission_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (mission_id, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"] or "{}")
            out.append(item)
        return out


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
class LongHorizonEngine:
    """Runs a mission DAG to completion across process lifetimes."""

    def __init__(
        self,
        *,
        project_root: Path | str,
        db_path: Path | str | None = None,
        executor: NodeExecutor | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        max_parallel: int = 3,
        entropy_threshold: int = 2,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        install_signal_handlers: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        resolved_db = Path(db_path) if db_path else self.project_root / ".unknown" / "memory" / "memory.db"
        self.store = MissionStore(resolved_db)
        self.executor = executor
        self.heartbeat_interval = max(1.0, heartbeat_interval)
        self.max_parallel = max(1, max_parallel)
        self.entropy = EntropyDetector(threshold=entropy_threshold)
        self._on_event = on_event
        self._install_signals = install_signal_handlers

        self._mission: Mission | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._phase = "idle"
        self._replan_hook: Callable[[MissionNode, Mission, list[str]], Sequence[MissionNode]] | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def mission(self) -> Mission | None:
        return self._mission

    def set_replan_hook(
        self, hook: Callable[[MissionNode, Mission, list[str]], Sequence[MissionNode]]
    ) -> None:
        """Called when entropy freezes a node, to obtain an alternative path."""

        self._replan_hook = hook

    def start_mission(
        self,
        goal: str,
        nodes: Iterable[MissionNode],
        *,
        mission_id: str = "",
        meta: Mapping[str, Any] | None = None,
    ) -> Mission:
        """Create and persist a new mission before any work begins."""

        now = time.time()
        mission = Mission(
            id=mission_id or f"m-{uuid.uuid4().hex[:12]}",
            goal=goal,
            cwd=str(self.project_root),
            status=MissionStatus.PENDING,
            created_at=now,
            updated_at=now,
            meta=dict(meta or {}),
        )
        for node in nodes:
            if node.key in mission.nodes:
                raise EngineError(f"duplicate node key {node.key!r}")
            mission.nodes[node.key] = node

        self._validate_dag(mission)
        for node in mission.nodes.values():
            if not node.depends_on:
                node.status = NodeStatus.READY

        self.store.create_mission(mission)
        self.store.record_event(mission.id, "mission.created", goal=goal, nodes=len(mission.nodes))
        self._mission = mission
        self._emit("mission.created", {"mission": mission.id, "goal": goal, "nodes": len(mission.nodes)})
        log.info("mission %s created with %d node(s)", mission.id, len(mission.nodes))
        return mission

    @staticmethod
    def _validate_dag(mission: Mission) -> None:
        """Reject a plan that cannot terminate, before it costs anything."""

        for node in mission.nodes.values():
            for dep in node.depends_on:
                if dep not in mission.nodes:
                    raise EngineError(f"node {node.key!r} depends on unknown node {dep!r}")

        # Kahn's algorithm: anything left over is in a cycle.
        indegree = {key: len(node.depends_on) for key, node in mission.nodes.items()}
        queue = [key for key, degree in indegree.items() if degree == 0]
        seen = 0
        while queue:
            current = queue.pop()
            seen += 1
            for key, node in mission.nodes.items():
                if current in node.depends_on:
                    indegree[key] -= 1
                    if indegree[key] == 0:
                        queue.append(key)
        if seen != len(mission.nodes):
            stuck = [key for key, degree in indegree.items() if degree > 0]
            raise EngineError(f"mission DAG contains a cycle involving: {', '.join(sorted(stuck))}")

    # -- resumption --------------------------------------------------------
    def resume_mission(self, mission_id: str) -> ResumeReport:
        """Reload a mission and reconcile it against what is actually on disk.

        This is the method that makes a three-week mission survive a reboot.
        It never trusts a stored status on its own: a node is only skipped when
        its recorded outputs are still byte-identical on disk.
        """

        mission = self.store.load_mission(mission_id)
        if mission is None:
            raise EngineError(f"mission {mission_id!r} not found in {self.store.path}")

        report = ResumeReport(mission_id=mission_id, resumed=True)

        beat = self.store.last_beat(mission_id)
        if beat:
            report.previous_owner_pid = int(beat["pid"])
            age = time.time() - float(beat["ts"])
            stale_after = self.heartbeat_interval * STALE_HEARTBEAT_FACTOR
            if age < stale_after and int(beat["pid"]) != os.getpid():
                if _pid_alive(int(beat["pid"])):
                    raise EngineError(
                        f"mission {mission_id} is still owned by live pid {beat['pid']} "
                        f"(heartbeat {age:.0f}s ago); attach instead of resuming"
                    )
            report.notes.append(
                f"previous owner pid {beat['pid']} last beat {age:.0f}s ago in phase {beat['phase'] or 'unknown'}"
            )

        drift = self.verify_disk_state(mission)
        report.drift = drift
        drifted = set(drift.modified) | set(drift.deleted)

        for node in mission.nodes.values():
            if node.status is NodeStatus.RUNNING:
                # The process died inside this node. Its attempt already counted.
                node.status = NodeStatus.READY if node.can_retry else NodeStatus.FAILED
                node.error = node.error or "process terminated while this node was running"
                node.note = "reopened after crash"
                report.reopened_crashed.append(node.key)
                self.store.save_node(mission_id, node)
                self.store.record_event(mission_id, "node.reopened", node_key=node.key, reason="crash")
                continue

            if node.status in {NodeStatus.CONSOLIDATED, NodeStatus.DONE_UNVERIFIED}:
                node_files = set(node.outputs)
                if node_files & drifted:
                    node.status = NodeStatus.READY
                    node.note = "reopened: outputs changed on disk since consolidation"
                    report.reopened_drifted.append(node.key)
                    self.store.save_node(mission_id, node)
                    self.store.record_event(
                        mission_id, "node.reopened", node_key=node.key, reason="drift"
                    )
                else:
                    if node.status is NodeStatus.DONE_UNVERIFIED:
                        node.status = NodeStatus.CONSOLIDATED
                        node.note = "verified on resume"
                        self.store.save_node(mission_id, node)
                    report.skipped_consolidated.append(node.key)
                continue

            if node.status is NodeStatus.FROZEN:
                report.frozen.append(node.key)

        # Recompute readiness now that statuses settled.
        for node in mission.nodes.values():
            if node.status is NodeStatus.BLOCKED:
                deps_ok = all(
                    mission.nodes[dep].status.terminal
                    for dep in node.depends_on
                    if dep in mission.nodes
                )
                if deps_ok:
                    node.status = NodeStatus.READY
                    self.store.save_node(mission_id, node)

        mission.status = MissionStatus.RUNNING
        self.store.set_mission_status(mission_id, MissionStatus.RUNNING)
        self._mission = mission
        # Splatting the report would collide with the ``mission_id`` parameter,
        # so the whole account goes in as one payload field.
        self.store.record_event(mission_id, "mission.resumed", report=report.to_dict())
        self._emit("mission.resumed", report.to_dict())
        log.info("%s", report.summary())
        return report

    def verify_disk_state(self, mission: Mission) -> DriftReport:
        """Re-hash every recorded file to see what moved while we were away."""

        report = DriftReport()
        for fingerprint in self.store.fingerprints(mission.id):
            path = self.project_root / fingerprint.path
            if not path.exists():
                report.deleted.append(fingerprint.path)
                continue
            try:
                current = hash_file(path)
            except OSError as exc:
                log.warning("cannot hash %s: %s", path, exc)
                report.modified.append(fingerprint.path)
                continue
            if current == fingerprint.sha256:
                report.unchanged.append(fingerprint.path)
            else:
                report.modified.append(fingerprint.path)
        return report

    def record_outputs(self, node: MissionNode, files: Iterable[str]) -> list[FileFingerprint]:
        """Hash and store what a node produced, so resume can verify it."""

        mission = self._require_mission()
        recorded: list[FileFingerprint] = []
        for raw in files:
            relative = raw
            path = Path(raw)
            if path.is_absolute():
                with contextlib.suppress(ValueError):
                    relative = str(path.relative_to(self.project_root))
            else:
                path = self.project_root / raw
            if not path.is_file():
                continue
            try:
                fingerprint = FileFingerprint(
                    path=relative,
                    sha256=hash_file(path),
                    size=path.stat().st_size,
                    recorded_at=time.time(),
                )
            except OSError as exc:
                log.warning("cannot fingerprint %s: %s", path, exc)
                continue
            self.store.record_fingerprint(mission.id, node.key, fingerprint)
            recorded.append(fingerprint)
        return recorded

    # -- heartbeat ---------------------------------------------------------
    async def _heartbeat_loop(self, mission_id: str) -> None:
        pid = os.getpid()
        try:
            while not self._stopping.is_set():
                self.store.beat(mission_id, pid=pid, phase=self._phase)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.heartbeat_interval)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise
        except Exception as exc:  # noqa: BLE001 - a dead heartbeat must not kill the mission
            log.warning("heartbeat stopped: %s", exc)

    def _install_signal_handlers(self) -> None:
        """Turn SIGTERM/SIGINT into a clean pause instead of data loss."""

        if not self._install_signals:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop yet
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, ValueError, AttributeError):
                loop.add_signal_handler(sig, self._request_stop, sig.name)

    def _request_stop(self, reason: str = "signal") -> None:
        if not self._stopping.is_set():
            log.info("stop requested (%s): finishing in-flight nodes and checkpointing", reason)
            self._stopping.set()
            if self._mission is not None:
                self.store.record_event(self._mission.id, "mission.stop_requested", reason=reason)

    def request_stop(self, reason: str = "user") -> None:
        """Ask the engine to pause at the next safe point."""

        self._request_stop(reason)

    # -- execution ---------------------------------------------------------
    def _require_mission(self) -> Mission:
        if self._mission is None:
            raise EngineError("no mission is loaded; call start_mission or resume_mission first")
        return self._mission

    async def run(self, *, max_nodes: int = 0) -> Mission:
        """Drive the DAG until it completes, stalls, or is asked to stop."""

        mission = self._require_mission()
        if self.executor is None:
            raise EngineError("no executor configured; pass executor= to LongHorizonEngine")

        self._stopping.clear()
        self._install_signal_handlers()
        mission.status = MissionStatus.RUNNING
        self.store.set_mission_status(mission.id, MissionStatus.RUNNING)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(mission.id))

        processed = 0
        semaphore = asyncio.Semaphore(self.max_parallel)
        try:
            while not self._stopping.is_set():
                ready = [node for node in mission.ready_nodes() if node.status is NodeStatus.READY]
                if not ready:
                    if self._all_settled(mission):
                        break
                    if not self._any_in_flight(mission):
                        # Nothing runnable and nothing running: the DAG is stuck
                        # behind failures. Say so rather than spinning.
                        break
                    await asyncio.sleep(0.05)
                    continue

                batch = ready[: self.max_parallel]
                if max_nodes:
                    remaining = max_nodes - processed
                    if remaining <= 0:
                        break
                    batch = batch[:remaining]

                await asyncio.gather(
                    *(self._run_node(mission, node, semaphore) for node in batch)
                )
                processed += len(batch)
        finally:
            await self._shutdown(mission)

        self._finalise(mission)
        return mission

    def _all_settled(self, mission: Mission) -> bool:
        return all(
            node.status in {NodeStatus.CONSOLIDATED, NodeStatus.SKIPPED, NodeStatus.FAILED, NodeStatus.FROZEN}
            for node in mission.nodes.values()
        )

    @staticmethod
    def _any_in_flight(mission: Mission) -> bool:
        return any(node.status is NodeStatus.RUNNING for node in mission.nodes.values())

    async def _run_node(self, mission: Mission, node: MissionNode, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            if self._stopping.is_set():
                return
            assert self.executor is not None  # guarded in run()

            node.status = NodeStatus.RUNNING
            node.attempts += 1
            node.started_at = node.started_at or time.time()
            node.error = ""
            self._phase = f"node:{node.key}"
            self.store.save_node(mission.id, node)
            self.store.record_event(
                mission.id, "node.started", node_key=node.key, attempt=node.attempts, role=node.role
            )
            self._emit("node.started", {"node": node.key, "attempt": node.attempts, "role": node.role})

            try:
                outcome = await self.executor.execute(node, mission)
            except asyncio.CancelledError:
                node.status = NodeStatus.READY if node.can_retry else NodeStatus.FAILED
                node.note = "cancelled"
                self.store.save_node(mission.id, node)
                raise
            except Exception as exc:  # noqa: BLE001 - executor faults must not kill the mission
                outcome = NodeOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
                log.exception("node %s raised", node.key)

            self._consolidate(mission, node, outcome)

    def _consolidate(self, mission: Mission, node: MissionNode, outcome: NodeOutcome) -> None:
        """Apply an outcome to a node, atomically, with entropy accounting."""

        verdict, mark = self.entropy.observe(node.key, outcome)
        node.observation = mark
        node.result = outcome.to_dict()
        node.finished_at = time.time()

        if outcome.touched_files:
            self.record_outputs(node, outcome.touched_files)
            node.outputs = tuple(sorted(set(node.outputs) | set(outcome.touched_files)))

        if outcome.ok:
            node.status = NodeStatus.CONSOLIDATED
            node.note = outcome.summary[:400]
            self.entropy.reset(node.key)
            self.store.save_node(mission.id, node)
            self.store.record_event(
                mission.id, "node.consolidated", node_key=node.key, summary=outcome.summary
            )
            self._emit("node.consolidated", {"node": node.key, "summary": outcome.summary})
            self._unblock_dependents(mission, node)
            return

        node.error = outcome.error or "node failed without an error message"

        if verdict is EntropyVerdict.STALLED:
            node.status = NodeStatus.FROZEN
            node.note = (
                f"frozen by entropy detector: {node.attempts} attempts produced identical "
                f"observations ({mark}) — the approach is not moving the world"
            )
            self.store.save_node(mission.id, node)
            self.store.record_event(
                mission.id,
                "node.frozen",
                node_key=node.key,
                reason="entropy",
                observation=mark,
                attempts=node.attempts,
            )
            self._emit("node.frozen", {"node": node.key, "reason": "entropy", "attempts": node.attempts})
            log.warning("node %s frozen: %s", node.key, node.note)
            self._attempt_replan(mission, node)
            return

        if node.can_retry:
            node.status = NodeStatus.READY
            node.note = f"retry {node.attempts}/{node.max_attempts}"
            if verdict is EntropyVerdict.REPEATING:
                node.note += " (repeating observation — next identical attempt freezes it)"
        else:
            node.status = NodeStatus.FAILED
            node.note = f"exhausted {node.max_attempts} attempt(s)"

        self.store.save_node(mission.id, node)
        self.store.record_event(
            mission.id,
            "node.failed",
            node_key=node.key,
            error=node.error,
            attempt=node.attempts,
            will_retry=node.status is NodeStatus.READY,
        )
        self._emit(
            "node.failed",
            {"node": node.key, "error": node.error, "will_retry": node.status is NodeStatus.READY},
        )

    def _attempt_replan(self, mission: Mission, node: MissionNode) -> None:
        """Ask the host for an alternative path around a frozen node."""

        if self._replan_hook is None:
            return
        try:
            replacements = list(self._replan_hook(node, mission, self.entropy.history(node.key)))
        except Exception as exc:  # noqa: BLE001 - a bad hook must not kill the mission
            log.warning("replan hook failed for %s: %s", node.key, exc)
            return
        if not replacements:
            return

        for replacement in replacements:
            if replacement.key in mission.nodes:
                continue
            replacement.status = NodeStatus.BLOCKED if replacement.depends_on else NodeStatus.READY
            mission.nodes[replacement.key] = replacement
            self.store.save_node(mission.id, replacement)
        node.status = NodeStatus.SKIPPED
        node.note += f"; replaced by {', '.join(item.key for item in replacements)}"
        self.store.save_node(mission.id, node)
        self.store.record_event(
            mission.id,
            "mission.replanned",
            node_key=node.key,
            replacements=[item.key for item in replacements],
        )
        self.entropy.reset(node.key)
        self._emit(
            "mission.replanned",
            {"frozen": node.key, "replacements": [item.key for item in replacements]},
        )
        self._unblock_dependents(mission, node)

    def _unblock_dependents(self, mission: Mission, finished: MissionNode) -> None:
        for candidate in mission.nodes.values():
            if candidate.status is not NodeStatus.BLOCKED:
                continue
            if finished.key not in candidate.depends_on:
                continue
            if all(
                mission.nodes[dep].status.terminal
                for dep in candidate.depends_on
                if dep in mission.nodes
            ):
                candidate.status = NodeStatus.READY
                self.store.save_node(mission.id, candidate)

    async def _shutdown(self, mission: Mission) -> None:
        self._stopping.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        self._phase = "idle"
        self.store.checkpoint(mission.id, "shutdown", mission.to_dict())

    def _finalise(self, mission: Mission) -> None:
        nodes = list(mission.nodes.values())
        failed = [node for node in nodes if node.status is NodeStatus.FAILED]
        frozen = [node for node in nodes if node.status is NodeStatus.FROZEN]
        pending = [
            node
            for node in nodes
            if node.status in {NodeStatus.READY, NodeStatus.BLOCKED, NodeStatus.RUNNING}
        ]

        if failed or frozen:
            status = MissionStatus.FAILED
        elif pending:
            # Stopped early: paused, not failed. Resuming picks these up.
            status = MissionStatus.PAUSED
        else:
            status = MissionStatus.COMPLETED

        mission.status = status
        self.store.set_mission_status(mission.id, status)
        self.store.record_event(
            mission.id,
            "mission.finished",
            status=status.value,
            failed=[node.key for node in failed],
            frozen=[node.key for node in frozen],
            pending=[node.key for node in pending],
        )
        self._emit("mission.finished", {"mission": mission.id, "status": status.value})
        log.info("mission %s -> %s", mission.id, status.value)

    # -- observability -----------------------------------------------------
    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception as exc:  # noqa: BLE001 - a bad subscriber must not kill the mission
            log.debug("event subscriber raised for %s: %s", kind, exc)

    def snapshot(self) -> dict[str, Any]:
        """Everything the dashboard and TUI need, in one read."""

        mission = self._mission
        if mission is None:
            return {"mission": None, "missions": self.store.list_missions(limit=20)}
        done, total = mission.progress
        beat = self.store.last_beat(mission.id)
        return {
            "mission": mission.to_dict(),
            "progress": {"done": done, "total": total, "pct": round(100 * done / total, 1) if total else 0.0},
            "phase": self._phase,
            "heartbeat": beat,
            "entropy": {key: self.entropy.history(key) for key in mission.nodes},
            "stopping": self._stopping.is_set(),
        }

    def close(self) -> None:
        self.store.close()


def _pid_alive(pid: int) -> bool:
    """Is a process still running?  Used to distinguish crash from concurrency."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else.
        return True
    except OSError:
        return False
    return True
