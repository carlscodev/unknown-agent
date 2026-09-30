"""Durable asynchronous hook bus for runtime and extension events.

Events are redacted before SQLite persistence. Deliveries are at-least-once,
priority ordered, timeout bounded, cancellable, retryable and protected by a
persistent circuit breaker. Handler output crosses a strict Pydantic boundary.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import inspect
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from unknown_agent.observability.events import Event

DeliveryStatus = Literal["pending", "running", "succeeded", "failed", "dead", "cancelled"]
HookHandler = Callable[
    ["HookContext"], Coroutine[Any, Any, "HookResult | dict[str, Any] | None"]
]


class HookSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    event_patterns: list[str] = Field(min_length=1)
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=1.0, ge=0, le=300)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_reset_seconds: float = Field(default=60.0, gt=0, le=3600)

    def matches(self, event_type: str) -> bool:
        return any(fnmatch.fnmatchcase(event_type, pattern) for pattern in self.event_patterns)


class HookResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    message: str = Field(default="", max_length=4000)
    data: dict[str, Any] = Field(default_factory=dict)


class HookContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    delivery_id: str
    event_id: str
    event_type: str
    run_id: str = ""
    step: int = 0
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(ge=1)


class HookDelivery(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    event_id: str
    hook_name: str
    status: DeliveryStatus
    attempts: int
    max_attempts: int
    next_attempt_at: float
    error: str = ""
    result: HookResult | None = None
    created_at: float
    updated_at: float


class HookBus:
    """Persistent dispatcher whose sync publish path never blocks the agent."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._handlers: dict[str, tuple[HookSpec, HookHandler]] = {}
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._dispatch_tasks: set[asyncio.Task[Any]] = set()
        self._lock: asyncio.Lock | None = None
        self._closed = False
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS hook_specs (
                name TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hook_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                run_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hook_deliveries (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES hook_events(id) ON DELETE CASCADE,
                hook_name TEXT NOT NULL,
                priority INTEGER NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                next_attempt_at REAL NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(event_id, hook_name)
            );
            CREATE TABLE IF NOT EXISTS hook_circuits (
                hook_name TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL,
                open_until REAL NOT NULL,
                last_error TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        with self.connection:
            self.connection.execute(
                "UPDATE hook_deliveries SET status='pending', error='recovered interrupted delivery', "
                "next_attempt_at=?, updated_at=? WHERE status='running'",
                (time.time(), time.time()),
            )

    def register(self, spec: HookSpec, handler: HookHandler) -> None:
        if self._closed:
            raise RuntimeError("hook bus is closed")
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("hook handlers must be async for real timeout and cancellation semantics")
        if spec.name in self._handlers:
            raise ValueError(f"hook {spec.name!r} is already registered")
        self._handlers[spec.name] = (spec, handler)
        with self.connection:
            self.connection.execute(
                "INSERT INTO hook_specs(name, spec_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET spec_json=excluded.spec_json, "
                "updated_at=excluded.updated_at",
                (spec.name, _json(spec.model_dump(mode="json")), time.time()),
            )

    def unregister(self, name: str) -> None:
        self._handlers.pop(name, None)
        task_ids = [
            delivery_id
            for delivery_id, task in self._active.items()
            if not task.done() and self._delivery_hook(delivery_id) == name
        ]
        for delivery_id in task_ids:
            self.cancel(delivery_id)

    def publish(self, event: Event) -> str:
        """Persist a redacted event and enqueue matching registered hooks."""

        if self._closed:
            raise RuntimeError("hook bus is closed")
        matching = [
            item
            for item in sorted(
                self._handlers.values(), key=lambda item: (item[0].priority, item[0].name)
            )
            if item[0].matches(str(event.type))
        ]
        if not matching:
            return ""
        payload = json.loads(event.to_json())
        payload_json = _json(payload)
        event_id = f"evt_{uuid.uuid4().hex}"
        now = time.time()
        with self.connection:
            self.connection.execute(
                "INSERT INTO hook_events(id, event_type, run_id, step, payload_json, "
                "payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    str(payload.get("type", "log")),
                    str(payload.get("run_id", "")),
                    int(payload.get("step", 0)),
                    payload_json,
                    hashlib.sha256(payload_json.encode()).hexdigest(),
                    now,
                ),
            )
            for spec, _handler in matching:
                self.connection.execute(
                    "INSERT INTO hook_deliveries(id, event_id, hook_name, priority, status, "
                    "attempts, max_attempts, next_attempt_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
                    (
                        f"del_{uuid.uuid4().hex}",
                        event_id,
                        spec.name,
                        spec.priority,
                        spec.max_attempts,
                        now,
                        now,
                        now,
                    ),
                )
        self._schedule(self.dispatch_event(event_id))
        return event_id

    async def start(self) -> int:
        """Resume due deliveries after handlers have been registered."""

        return await self.drain()

    async def dispatch_event(self, event_id: str) -> int:
        if self._closed:
            return 0
        lock = self._lock
        if lock is None:
            lock = self._lock = asyncio.Lock()
        async with lock:
            rows = self.connection.execute(
                "SELECT id FROM hook_deliveries WHERE event_id=? AND status='pending' "
                "AND next_attempt_at<=? ORDER BY priority, created_at",
                (event_id, time.time()),
            ).fetchall()
            completed = 0
            for row in rows:
                if await self._deliver(str(row["id"])):
                    completed += 1
            return completed

    async def drain(self, *, limit: int = 1000) -> int:
        if self._closed:
            return 0
        rows = self.connection.execute(
            "SELECT id FROM hook_deliveries WHERE status='pending' AND next_attempt_at<=? "
            "ORDER BY priority, created_at LIMIT ?",
            (time.time(), max(1, min(limit, 10_000))),
        ).fetchall()
        completed = 0
        for row in rows:
            if await self._deliver(str(row["id"])):
                completed += 1
        return completed

    async def _deliver(self, delivery_id: str) -> bool:
        row = self.connection.execute(
            "SELECT * FROM hook_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        if row is None or row["status"] != "pending":
            return False
        hook_name = str(row["hook_name"])
        registered = self._handlers.get(hook_name)
        if registered is None:
            return False
        spec, handler = registered
        open_until = self._circuit_open_until(hook_name)
        if open_until > time.time():
            with self.connection:
                self.connection.execute(
                    "UPDATE hook_deliveries SET next_attempt_at=?, updated_at=? WHERE id=?",
                    (open_until, time.time(), delivery_id),
                )
            return False
        attempt = int(row["attempts"]) + 1
        try:
            event = self._event(str(row["event_id"]))
        except Exception as exc:  # noqa: BLE001 - corrupted durable state is contained
            self._fail(delivery_id, spec, attempt, f"event integrity error: {exc}")
            return False
        with self.connection:
            self.connection.execute(
                "UPDATE hook_deliveries SET status='running', attempts=?, updated_at=? WHERE id=?",
                (attempt, time.time(), delivery_id),
            )
        context = HookContext(
            delivery_id=delivery_id,
            event_id=str(row["event_id"]),
            event_type=str(event["type"]),
            run_id=str(event.get("run_id", "")),
            step=int(event.get("step", 0)),
            message=str(event.get("message", "")),
            data=dict(event.get("data") or {}),
            attempt=attempt,
        )
        task: asyncio.Task[HookResult | dict[str, Any] | None] = asyncio.create_task(
            handler(context), name=f"hook:{hook_name}:{delivery_id}"
        )
        self._active[delivery_id] = task
        try:
            raw = await asyncio.wait_for(task, timeout=spec.timeout_seconds)
            result = HookResult() if raw is None else HookResult.model_validate(raw)
            encoded = _json(result.model_dump(mode="json"))
            if len(encoded.encode()) > 64_000:
                raise ValueError("hook result exceeds 64000 bytes")
            if not result.ok:
                raise RuntimeError(result.message or "hook reported failure")
            with self.connection:
                self.connection.execute(
                    "UPDATE hook_deliveries SET status='succeeded', result_json=?, error='', "
                    "updated_at=? WHERE id=?",
                    (encoded, time.time(), delivery_id),
                )
                self._reset_circuit(hook_name)
            return True
        except asyncio.CancelledError:
            with self.connection:
                self.connection.execute(
                    "UPDATE hook_deliveries SET status='cancelled', error='cancelled', "
                    "updated_at=? WHERE id=?",
                    (time.time(), delivery_id),
                )
            return False
        except TimeoutError:
            self._fail(delivery_id, spec, attempt, "hook timed out")
            return False
        except Exception as exc:  # noqa: BLE001 - untrusted hook boundary
            self._fail(delivery_id, spec, attempt, f"{type(exc).__name__}: {exc}")
            return False
        finally:
            self._active.pop(delivery_id, None)

    def _fail(self, delivery_id: str, spec: HookSpec, attempt: int, error: str) -> None:
        error = error[:2000]
        failures = self._record_circuit_failure(spec, error)
        status: DeliveryStatus = "dead" if attempt >= spec.max_attempts else "pending"
        delay = spec.retry_backoff_seconds * (2 ** max(0, attempt - 1))
        if failures >= spec.circuit_failure_threshold:
            delay = max(delay, spec.circuit_reset_seconds)
        with self.connection:
            self.connection.execute(
                "UPDATE hook_deliveries SET status=?, attempts=?, error=?, next_attempt_at=?, "
                "updated_at=? WHERE id=?",
                (status, attempt, error, time.time() + delay, time.time(), delivery_id),
            )

    def cancel(self, delivery_id: str) -> HookDelivery:
        task = self._active.get(delivery_id)
        if task is not None and not task.done():
            task.cancel()
        else:
            with self.connection:
                self.connection.execute(
                    "UPDATE hook_deliveries SET status='cancelled', error='cancelled', "
                    "updated_at=? WHERE id=? AND status IN ('pending', 'running')",
                    (time.time(), delivery_id),
                )
        return self.delivery(delivery_id)

    def replay(self, delivery_id: str) -> HookDelivery:
        current = self.delivery(delivery_id)
        if current.status not in {"dead", "failed", "cancelled", "succeeded"}:
            raise ValueError(f"delivery {delivery_id} is {current.status}, not replayable")
        with self.connection:
            self.connection.execute(
                "UPDATE hook_deliveries SET status='pending', attempts=0, error='', result_json=NULL, "
                "next_attempt_at=?, updated_at=? WHERE id=?",
                (time.time(), time.time(), delivery_id),
            )
        self._schedule(self.dispatch_event(current.event_id))
        return self.delivery(delivery_id)

    def delivery(self, delivery_id: str) -> HookDelivery:
        row = self.connection.execute(
            "SELECT * FROM hook_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown hook delivery {delivery_id}")
        result = HookResult.model_validate_json(row["result_json"]) if row["result_json"] else None
        return HookDelivery(
            id=str(row["id"]),
            event_id=str(row["event_id"]),
            hook_name=str(row["hook_name"]),
            status=cast(DeliveryStatus, str(row["status"])),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            error=str(row["error"]),
            result=result,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def deliveries(self, event_id: str = "") -> list[HookDelivery]:
        if event_id:
            rows = self.connection.execute(
                "SELECT id FROM hook_deliveries WHERE event_id=? ORDER BY priority, created_at",
                (event_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT id FROM hook_deliveries ORDER BY created_at DESC"
            ).fetchall()
        return [self.delivery(str(row["id"])) for row in rows]

    def verify_event(self, event_id: str) -> bool:
        row = self.connection.execute(
            "SELECT payload_json, payload_sha256 FROM hook_events WHERE id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown hook event {event_id}")
        return hashlib.sha256(str(row["payload_json"]).encode()).hexdigest() == row["payload_sha256"]

    async def close(self) -> None:
        self._closed = True
        tasks = [*self._active.values(), *self._dispatch_tasks]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.connection.close()

    def _schedule(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coroutine.close()
            return
        task: asyncio.Task[Any] = loop.create_task(coroutine)
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    def _event(self, event_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT payload_json, payload_sha256 FROM hook_events WHERE id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown hook event {event_id}")
        raw = str(row["payload_json"])
        if hashlib.sha256(raw.encode()).hexdigest() != row["payload_sha256"]:
            raise ValueError(f"hook event {event_id} failed integrity verification")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"hook event {event_id} payload is not an object")
        return payload

    def _delivery_hook(self, delivery_id: str) -> str:
        row = self.connection.execute(
            "SELECT hook_name FROM hook_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        return str(row["hook_name"]) if row is not None else ""

    def _circuit_open_until(self, hook_name: str) -> float:
        row = self.connection.execute(
            "SELECT open_until FROM hook_circuits WHERE hook_name=?", (hook_name,)
        ).fetchone()
        return float(row["open_until"]) if row is not None else 0.0

    def _record_circuit_failure(self, spec: HookSpec, error: str) -> int:
        row = self.connection.execute(
            "SELECT consecutive_failures FROM hook_circuits WHERE hook_name=?", (spec.name,)
        ).fetchone()
        failures = (int(row["consecutive_failures"]) if row is not None else 0) + 1
        open_until = time.time() + spec.circuit_reset_seconds if failures >= spec.circuit_failure_threshold else 0.0
        with self.connection:
            self.connection.execute(
                "INSERT INTO hook_circuits(hook_name, consecutive_failures, open_until, "
                "last_error, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(hook_name) "
                "DO UPDATE SET consecutive_failures=excluded.consecutive_failures, "
                "open_until=excluded.open_until, last_error=excluded.last_error, "
                "updated_at=excluded.updated_at",
                (spec.name, failures, open_until, error, time.time()),
            )
        return failures

    def _reset_circuit(self, hook_name: str) -> None:
        self.connection.execute(
            "INSERT INTO hook_circuits(hook_name, consecutive_failures, open_until, "
            "last_error, updated_at) VALUES (?, 0, 0, '', ?) ON CONFLICT(hook_name) "
            "DO UPDATE SET consecutive_failures=0, open_until=0, last_error='', "
            "updated_at=excluded.updated_at",
            (hook_name, time.time()),
        )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DeliveryStatus",
    "HookBus",
    "HookContext",
    "HookDelivery",
    "HookHandler",
    "HookResult",
    "HookSpec",
]
