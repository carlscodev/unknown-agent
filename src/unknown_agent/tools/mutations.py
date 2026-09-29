"""Durable transactional queue for reversible tool mutations.

Transactions use optimistic path fingerprints, preflight every policy decision,
snapshot all affected paths before the first mutation, and roll back the complete
set in reverse order after failure or cancellation.  SQLite WAL records state and
a SHA-256 chained event stream so interrupted processes can recover safely.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from unknown_agent.core.models import RiskLevel, ToolCall, ToolResult
from unknown_agent.tools.base import Tool

TransactionStatus = Literal[
    "open", "applying", "committed", "rolling_back", "rolled_back", "cancelled", "failed"
]
OperationStatus = Literal["queued", "applied", "failed", "rolled_back"]
TERMINAL_STATUSES = {"committed", "rolled_back", "cancelled", "failed"}
PATH_ARGUMENTS = {"path", "file", "target", "source", "destination"}


class ExecutorProtocol(Protocol):
    registry: Any
    policy: Any
    context: Any

    async def execute_approved(
        self, call: ToolCall, tool: Tool, arguments: dict[str, Any]
    ) -> ToolResult: ...


class MutationOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    position: int = Field(ge=0)
    tool: str
    arguments: dict[str, Any]
    paths: list[str]
    preconditions: dict[str, str]
    status: OperationStatus = "queued"
    result: dict[str, Any] | None = None


class MutationTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    scope: str = ""
    label: str = ""
    status: TransactionStatus
    operations: list[MutationOperation] = Field(default_factory=list)
    error: str = ""
    created_at: float
    updated_at: float


class MutationQueue:
    """Persistent queue with optimistic concurrency and crash recovery."""

    def __init__(self, path: str | Path, *, project_root: str | Path) -> None:
        self.path = Path(path)
        self.project_root = Path(project_root).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_root = self.path.parent / "mutations"
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS mutation_transactions (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mutation_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT NOT NULL REFERENCES mutation_transactions(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                tool TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                paths_json TEXT NOT NULL,
                preconditions_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                UNIQUE(transaction_id, position)
            );
            CREATE TABLE IF NOT EXISTS mutation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT NOT NULL REFERENCES mutation_transactions(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(transaction_id, sequence)
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> MutationQueue:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def begin(self, *, scope: str = "", label: str = "") -> MutationTransaction:
        transaction_id = f"mut_{uuid.uuid4().hex[:16]}"
        now = time.time()
        with self.connection:
            self.connection.execute(
                "INSERT INTO mutation_transactions(id, scope, label, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'open', ?, ?)",
                (transaction_id, scope, label, now, now),
            )
            self._event(transaction_id, "transaction.opened", {"scope": scope, "label": label})
        return self.get(transaction_id)

    def stage(
        self,
        transaction_id: str,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        executor: ExecutorProtocol,
    ) -> MutationOperation:
        transaction = self.get(transaction_id)
        if transaction.status != "open":
            raise ValueError(f"transaction {transaction_id} is {transaction.status}, not open")
        if tool_name.startswith("mutation_"):
            raise ValueError("transaction management tools cannot be staged recursively")
        tool = executor.registry.maybe_get(tool_name)
        if tool is None:
            raise KeyError(f"unknown tool {tool_name!r}")
        mutating = tool.risk in {RiskLevel.WRITE, RiskLevel.DESTRUCTIVE}
        validating = tool.category == "validation" and tool.risk in {
            RiskLevel.READ,
            RiskLevel.EXECUTE,
        }
        if not mutating and not validating:
            raise ValueError(
                f"{tool_name} is neither a reversible mutation nor a validation gate"
            )
        cleaned, problems = tool.validate_arguments(arguments)
        hard = [problem for problem in problems if not problem.endswith("(ignored)")]
        if hard:
            raise ValueError("invalid arguments: " + "; ".join(hard))
        decision = executor.policy.evaluate(tool, cleaned)
        if not decision.allowed:
            raise PermissionError(decision.denied_reason)
        paths = self._paths(cleaned, executor)
        if mutating and not paths:
            raise ValueError(
                f"{tool_name} has no concrete path and cannot be rolled back transactionally"
            )
        preconditions = {path: _fingerprint(Path(path)) for path in paths}
        row = self.connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM mutation_operations WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()
        position = int(row[0]) if row is not None else 0
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO mutation_operations
                    (transaction_id, position, tool, arguments_json, paths_json,
                     preconditions_json, status)
                VALUES (?, ?, ?, ?, ?, ?, 'queued')
                """,
                (
                    transaction_id,
                    position,
                    tool_name,
                    _json(cleaned),
                    _json(paths),
                    _json(preconditions),
                ),
            )
            self._touch(transaction_id)
            self._event(
                transaction_id,
                "operation.staged",
                {"position": position, "tool": tool_name, "paths": paths},
            )
        operation_id = cursor.lastrowid
        if operation_id is None:  # pragma: no cover - sqlite contract guard
            raise sqlite3.IntegrityError("staged operation has no id")
        return self._operation_by_id(int(operation_id))

    def preview(self, transaction_id: str) -> MutationTransaction:
        transaction = self.get(transaction_id)
        for operation in transaction.operations:
            for path, expected in operation.preconditions.items():
                actual = _fingerprint(Path(path))
                if actual != expected:
                    raise RuntimeError(
                        f"precondition changed for {self._display_path(path)}: expected {expected}, got {actual}"
                    )
        return transaction

    async def commit(self, transaction_id: str, executor: ExecutorProtocol) -> MutationTransaction:
        transaction = self.get(transaction_id)
        if transaction.status == "committed":
            return transaction
        transaction = self.preview(transaction_id)
        if transaction.status != "open":
            raise ValueError(f"transaction {transaction_id} is {transaction.status}, not open")
        if not transaction.operations:
            raise ValueError("cannot commit an empty transaction")

        prepared: list[tuple[MutationOperation, Tool, dict[str, Any]]] = []
        for operation in transaction.operations:
            tool = executor.registry.get(operation.tool)
            decision = executor.policy.evaluate(tool, operation.arguments)
            if not decision.allowed:
                raise PermissionError(decision.denied_reason)
            if decision.requires_confirmation:
                decision = executor.policy.confirm(tool, operation.arguments, decision)
                if not decision.allowed:
                    raise PermissionError(decision.denied_reason)
            if decision.dry_run:
                raise ValueError("transaction commits are disabled while policy.dry_run is active")
            prepared.append((operation, tool, operation.arguments))

        self._snapshot(transaction)
        task = asyncio.current_task()
        if task is not None:
            self._tasks[transaction_id] = task
        self._set_status(transaction_id, "applying")
        self._event(transaction_id, "commit.started", {"operations": len(prepared)})
        try:
            for operation, tool, arguments in prepared:
                if self._cancel_requested(transaction_id):
                    raise asyncio.CancelledError
                call = ToolCall(
                    id=f"{transaction_id}_{operation.position}",
                    name=tool.name,
                    arguments=arguments,
                )
                result = await executor.execute_approved(call, tool, arguments)
                self._set_operation(
                    cast(int, operation.id),
                    "applied" if result.ok else "failed",
                    result.to_dict(),
                )
                self._event(
                    transaction_id,
                    "operation.applied" if result.ok else "operation.failed",
                    {"position": operation.position, "tool": tool.name, "ok": result.ok},
                )
                if not result.ok:
                    raise RuntimeError(result.error or result.output or f"{tool.name} failed")
            self._set_status(transaction_id, "committed")
            self._event(transaction_id, "commit.completed", {})
            return self.get(transaction_id)
        except asyncio.CancelledError:
            self._rollback(transaction_id, final_status="cancelled", error="commit cancelled")
            return self.get(transaction_id)
        except Exception as exc:
            self._rollback(transaction_id, final_status="rolled_back", error=f"{type(exc).__name__}: {exc}")
            return self.get(transaction_id)
        finally:
            self._tasks.pop(transaction_id, None)

    def cancel(self, transaction_id: str) -> MutationTransaction:
        transaction = self.get(transaction_id)
        if transaction.status in TERMINAL_STATUSES:
            return transaction
        self._event(transaction_id, "cancel.requested", {})
        if transaction.status == "open":
            self._set_status(transaction_id, "cancelled", error="cancelled before commit")
        else:
            task = self._tasks.get(transaction_id)
            if task is not None and not task.done():
                task.cancel()
        return self.get(transaction_id)

    def recover(self) -> list[MutationTransaction]:
        rows = self.connection.execute(
            "SELECT id FROM mutation_transactions WHERE status IN ('applying', 'rolling_back') ORDER BY created_at"
        ).fetchall()
        recovered: list[MutationTransaction] = []
        for row in rows:
            transaction_id = str(row["id"])
            snapshot = self.snapshot_root / transaction_id / "manifest.json"
            if snapshot.exists():
                self._rollback(
                    transaction_id,
                    final_status="rolled_back",
                    error="recovered interrupted transaction",
                )
            else:
                self._set_status(
                    transaction_id,
                    "failed",
                    error="interrupted transaction has no snapshot manifest",
                )
                self._event(transaction_id, "recovery.failed", {"reason": "missing snapshot"})
            recovered.append(self.get(transaction_id))
        return recovered

    def get(self, transaction_id: str) -> MutationTransaction:
        row = self.connection.execute(
            "SELECT * FROM mutation_transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown mutation transaction {transaction_id}")
        operations = self.connection.execute(
            "SELECT * FROM mutation_operations WHERE transaction_id=? ORDER BY position",
            (transaction_id,),
        ).fetchall()
        return MutationTransaction(
            id=str(row["id"]),
            scope=str(row["scope"]),
            label=str(row["label"]),
            status=cast(TransactionStatus, str(row["status"])),
            operations=[self._operation(item) for item in operations],
            error=str(row["error"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def transactions(self, *, scope: str = "", limit: int = 100) -> list[MutationTransaction]:
        if scope:
            rows = self.connection.execute(
                "SELECT id FROM mutation_transactions WHERE scope=? ORDER BY created_at DESC LIMIT ?",
                (scope, max(1, min(limit, 1000))),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT id FROM mutation_transactions ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [self.get(str(row["id"])) for row in rows]

    def verify_events(self, transaction_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT * FROM mutation_events WHERE transaction_id=? ORDER BY sequence",
            (transaction_id,),
        ).fetchall()
        errors: list[str] = []
        previous = ""
        for row in rows:
            body = _event_body(
                transaction_id,
                int(row["sequence"]),
                str(row["kind"]),
                str(row["payload_json"]),
                previous,
                float(row["created_at"]),
            )
            actual = hashlib.sha256(body.encode()).hexdigest()
            if row["previous_hash"] != previous:
                errors.append(f"event {row['id']} previous hash mismatch")
            if row["event_hash"] != actual:
                errors.append(f"event {row['id']} content hash mismatch")
            previous = str(row["event_hash"])
        return errors

    def _paths(self, arguments: dict[str, Any], executor: ExecutorProtocol) -> list[str]:
        raw_paths = [
            value
            for key, value in arguments.items()
            if key in PATH_ARGUMENTS and isinstance(value, str) and value
        ]
        raw_paths.extend(
            value for value in (arguments.get("paths") or []) if isinstance(value, str)
        )
        resolved: list[str] = []
        for raw in raw_paths:
            path = executor.policy.resolve_path(raw)
            decision = executor.policy.check_path(path, mutating=True)
            if not decision.allowed:
                raise PermissionError(decision.denied_reason)
            text = str(path)
            if text not in resolved:
                resolved.append(text)
        return resolved

    def _snapshot(self, transaction: MutationTransaction) -> None:
        directory = self.snapshot_root / transaction.id
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        paths: list[str] = []
        for operation in transaction.operations:
            for raw in operation.paths:
                path = Path(raw)
                missing_parents: list[str] = []
                parent = path.parent
                while parent != self.project_root and self.project_root in parent.parents:
                    if parent.exists() or parent.is_symlink():
                        break
                    missing_parents.append(str(parent))
                    parent = parent.parent
                for candidate in [*reversed(missing_parents), raw]:
                    if candidate not in paths:
                        paths.append(candidate)
        manifest: list[dict[str, Any]] = []
        for index, raw in enumerate(paths):
            source = Path(raw)
            entry: dict[str, Any] = {
                "path": raw,
                "existed": source.exists() or source.is_symlink(),
                "kind": "missing",
                "snapshot": str(index),
            }
            target = directory / str(index)
            if source.is_symlink():
                entry["kind"] = "symlink"
                entry["link"] = os.readlink(source)
            elif source.is_dir():
                entry["kind"] = "directory"
                shutil.copytree(source, target, symlinks=True)
            elif source.exists():
                entry["kind"] = "file"
                shutil.copy2(source, target)
            manifest.append(entry)
        _atomic_write(directory / "manifest.json", _json(manifest))
        self._event(transaction.id, "snapshot.created", {"paths": len(manifest)})

    def _rollback(
        self,
        transaction_id: str,
        *,
        final_status: Literal["rolled_back", "cancelled"],
        error: str,
    ) -> None:
        self._set_status(transaction_id, "rolling_back", error=error)
        directory = self.snapshot_root / transaction_id
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            self._set_status(transaction_id, "failed", error=error + "; snapshot manifest missing")
            self._event(transaction_id, "rollback.failed", {"reason": "missing snapshot"})
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in reversed(manifest):
            target = Path(entry["path"])
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            if not entry["existed"]:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            snapshot = directory / entry["snapshot"]
            if entry["kind"] == "symlink":
                target.symlink_to(entry["link"])
            elif entry["kind"] == "directory":
                shutil.copytree(snapshot, target, symlinks=True)
            elif entry["kind"] == "file":
                shutil.copy2(snapshot, target)
        with self.connection:
            self.connection.execute(
                "UPDATE mutation_operations SET status='rolled_back' "
                "WHERE transaction_id=? AND status='applied'",
                (transaction_id,),
            )
        self._set_status(transaction_id, final_status, error=error)
        self._event(transaction_id, "rollback.completed", {"final_status": final_status, "error": error})

    def _cancel_requested(self, transaction_id: str) -> bool:
        row = self.connection.execute(
            "SELECT kind FROM mutation_events WHERE transaction_id=? ORDER BY sequence DESC LIMIT 1",
            (transaction_id,),
        ).fetchone()
        return row is not None and row["kind"] == "cancel.requested"

    def _set_status(self, transaction_id: str, status: TransactionStatus, *, error: str = "") -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE mutation_transactions SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, time.time(), transaction_id),
            )

    def _set_operation(self, operation_id: int, status: OperationStatus, result: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE mutation_operations SET status=?, result_json=? WHERE id=?",
                (status, _json(result), operation_id),
            )

    def _touch(self, transaction_id: str) -> None:
        self.connection.execute(
            "UPDATE mutation_transactions SET updated_at=? WHERE id=?",
            (time.time(), transaction_id),
        )

    def _event(self, transaction_id: str, kind: str, payload: dict[str, Any]) -> None:
        row = self.connection.execute(
            "SELECT sequence, event_hash FROM mutation_events WHERE transaction_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (transaction_id,),
        ).fetchone()
        sequence = int(row["sequence"]) + 1 if row is not None else 1
        previous = str(row["event_hash"]) if row is not None else ""
        payload_json = _json(payload)
        created_at = time.time()
        body = _event_body(transaction_id, sequence, kind, payload_json, previous, created_at)
        event_hash = hashlib.sha256(body.encode()).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO mutation_events(transaction_id, sequence, kind, payload_json, "
                "previous_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (transaction_id, sequence, kind, payload_json, previous, event_hash, created_at),
            )

    def _operation_by_id(self, operation_id: int) -> MutationOperation:
        row = self.connection.execute(
            "SELECT * FROM mutation_operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown mutation operation {operation_id}")
        return self._operation(row)

    @staticmethod
    def _operation(row: sqlite3.Row) -> MutationOperation:
        return MutationOperation(
            id=int(row["id"]),
            position=int(row["position"]),
            tool=str(row["tool"]),
            arguments=json.loads(row["arguments_json"]),
            paths=json.loads(row["paths_json"]),
            preconditions=json.loads(row["preconditions_json"]),
            status=cast(OperationStatus, str(row["status"])),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
        )

    def _display_path(self, path: str) -> str:
        try:
            return str(Path(path).relative_to(self.project_root))
        except ValueError:
            return path


def _fingerprint(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"file\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "file:" + digest.hexdigest()
    digest.update(b"directory\0")
    for item in sorted(path.rglob("*"), key=lambda candidate: candidate.as_posix()):
        relative = item.relative_to(path).as_posix().encode()
        digest.update(relative + b"\0")
        if item.is_symlink():
            digest.update(b"link\0" + os.readlink(item).encode())
        elif item.is_file():
            with item.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return "directory:" + digest.hexdigest()


def _event_body(
    transaction_id: str,
    sequence: int,
    kind: str,
    payload_json: str,
    previous_hash: str,
    created_at: float,
) -> str:
    return "\0".join(
        (transaction_id, str(sequence), kind, payload_json, previous_hash, repr(created_at))
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


__all__ = ["MutationOperation", "MutationQueue", "MutationTransaction"]
