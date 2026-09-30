"""Persistent, lease-based scheduler for sovereign long-running automation.

The scheduler deliberately owns persistence and dispatch, but not business logic.
Callers register async actions, so the same execution engine can invoke the agent,
a signed extension, or an application action without introducing a second runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from unknown_agent.config.secrets import is_reference, resolve_secret

_JSON_LIMIT = 1_048_576
_OUTPUT_LIMIT = 65_536


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _required_dt(value: str) -> datetime:
    parsed = _dt(value)
    if parsed is None:  # Defensive: persisted NOT NULL columns must remain valid.
        raise ValueError("required scheduler timestamp is missing")
    return parsed


def _resolve_secret(reference: str) -> str:
    value = resolve_secret(reference)
    if value is None:
        raise ValueError(f"secret reference did not resolve: {reference}")
    return value


def _json(value: Any, *, limit: int = _JSON_LIMIT) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode()) > limit:
        raise ValueError(f"structured value exceeds {limit} bytes")
    return encoded


class TriggerKind(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"
    WEBHOOK = "webhook"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"
    CANCELLED = "cancelled"


class JobSpec(BaseModel):
    """Validated user-controlled job definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    trigger: TriggerKind
    action: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    run_at: datetime | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    cron: str | None = Field(default=None, max_length=256)
    timezone: str = Field(default="UTC", max_length=128)
    webhook_secret_ref: str | None = Field(default=None, max_length=1024)
    max_attempts: int = Field(default=3, ge=1, le=100)
    backoff_seconds: float = Field(default=5, ge=0.1, le=86_400)
    timeout_seconds: float = Field(default=300, ge=0.1, le=300)
    max_concurrency: int = Field(default=1, ge=1, le=100)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_trigger(self) -> JobSpec:
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        fields = (self.run_at, self.interval_seconds, self.cron, self.webhook_secret_ref)
        required = {
            TriggerKind.ONCE: self.run_at,
            TriggerKind.INTERVAL: self.interval_seconds,
            TriggerKind.CRON: self.cron,
            TriggerKind.WEBHOOK: self.webhook_secret_ref,
        }
        if required[self.trigger] is None:
            raise ValueError(f"{self.trigger.value} trigger is missing its required setting")
        expected = list(TriggerKind).index(self.trigger)
        if any(value is not None for index, value in enumerate(fields) if index != expected):
            raise ValueError("trigger-specific settings may not be mixed")
        if self.run_at is not None and self.run_at.tzinfo is None:
            raise ValueError("run_at must include a timezone")
        if self.cron is not None and not croniter.is_valid(self.cron):
            raise ValueError("invalid cron expression")
        if self.webhook_secret_ref is not None:
            if not is_reference(self.webhook_secret_ref):
                raise ValueError("webhook signing secrets must use a secret reference")
            if not self.webhook_secret_ref.partition(":")[2].strip():
                raise ValueError("webhook secret reference target is empty")
        _ = zone
        _json(self.payload)
        return self


class Job(JobSpec):
    id: str
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None


class ActionRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    source: str
    registered_at: datetime
    last_seen_at: datetime
    available: bool


class ExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    job_id: str
    occurrence_key: str
    attempt: int
    action: str
    payload: dict[str, Any]
    scheduled_for: datetime


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool = True
    output: dict[str, Any] = Field(default_factory=dict)
    message: str = Field(default="", max_length=4096)

    @model_validator(mode="after")
    def bound_output(self) -> ActionResult:
        _json(self.output, limit=_OUTPUT_LIMIT)
        return self


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    job_id: str
    occurrence_key: str
    status: ExecutionStatus
    scheduled_for: datetime
    next_attempt_at: datetime
    attempts: int
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


Action = Callable[[ExecutionContext], Awaitable[ActionResult | Mapping[str, Any] | None]]


class Scheduler:
    """SQLite/WAL scheduler with transactional claims and expiring leases."""

    def __init__(
        self,
        path: Path,
        *,
        owner: str | None = None,
        lease_seconds: float = 60,
        secret_resolver: Callable[[str], str] = _resolve_secret,
    ) -> None:
        self.path = path
        self.owner = owner or f"scheduler-{uuid.uuid4()}"
        self.lease_seconds = max(5.0, lease_seconds)
        self.secret_resolver = secret_resolver
        self.actions: dict[str, Action] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_jobs (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, enabled INTEGER NOT NULL,
                    next_run_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scheduler_jobs_due
                    ON scheduler_jobs(enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS scheduler_actions (
                    name TEXT PRIMARY KEY, source TEXT NOT NULL,
                    registered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_executions (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES scheduler_jobs(id),
                    occurrence_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL, next_attempt_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
                    lease_owner TEXT, lease_until TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scheduler_executions_due
                    ON scheduler_executions(status, next_attempt_at, lease_until);
                CREATE TABLE IF NOT EXISTS scheduler_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
                    subject TEXT NOT NULL, detail TEXT NOT NULL, previous_hash TEXT NOT NULL,
                    record_hash TEXT NOT NULL UNIQUE
                );
                """
            )

    def register_action(self, name: str, action: Action, *, source: str = "application") -> None:
        if not name or name in self.actions:
            raise ValueError(f"action is invalid or already registered: {name!r}")
        if not inspect.iscoroutinefunction(action):
            raise TypeError("scheduler actions must be async functions")
        if not source or len(source) > 256:
            raise ValueError("action source must be between 1 and 256 characters")
        now = _iso(_now())
        with self._transaction() as db:
            db.execute(
                """INSERT INTO scheduler_actions(name,source,registered_at,last_seen_at)
                   VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                   source=excluded.source,last_seen_at=excluded.last_seen_at""",
                (name, source, now, now),
            )
            self._audit(db, "action.registered", name, {"source": source})
        self.actions[name] = action

    def list_actions(self) -> list[ActionRegistration]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM scheduler_actions ORDER BY name").fetchall()
        return [
            ActionRegistration(
                name=row["name"],
                source=row["source"],
                registered_at=_required_dt(row["registered_at"]),
                last_seen_at=_required_dt(row["last_seen_at"]),
                available=row["name"] in self.actions,
            )
            for row in rows
        ]

    def create_job(self, spec: JobSpec, *, job_id: str | None = None) -> Job:
        identifier = job_id or str(uuid.uuid4())
        now = _now()
        next_run = self._first_run(spec, now) if spec.enabled else None
        with self._transaction() as db:
            db.execute(
                "INSERT INTO scheduler_jobs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    spec.model_dump_json(),
                    int(spec.enabled),
                    _iso(next_run),
                    _iso(now),
                    _iso(now),
                ),
            )
            self._audit(db, "job.created", identifier, spec.model_dump(mode="json"))
        return Job(
            **spec.model_dump(), id=identifier, created_at=now, updated_at=now, next_run_at=next_run
        )

    def get_job(self, job_id: str) -> Job:
        with self._connect() as db:
            row = db.execute("SELECT * FROM scheduler_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def list_jobs(self) -> list[Job]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM scheduler_jobs ORDER BY created_at, id").fetchall()
        return [self._job(row) for row in rows]

    def set_enabled(self, job_id: str, enabled: bool) -> Job:
        now = _now()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM scheduler_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            spec = JobSpec.model_validate_json(row["spec"])
            next_run = self._first_run(spec, now) if enabled else None
            db.execute(
                "UPDATE scheduler_jobs SET enabled=?, next_run_at=?, updated_at=? WHERE id=?",
                (int(enabled), _iso(next_run), _iso(now), job_id),
            )
            self._audit(db, "job.resumed" if enabled else "job.paused", job_id, {})
        return self.get_job(job_id)

    def trigger(self, job_id: str, *, idempotency_key: str | None = None) -> ExecutionRecord:
        job = self.get_job(job_id)
        if not job.enabled:
            raise ValueError("job is paused")
        now = _now()
        if idempotency_key is not None:
            if not idempotency_key or len(idempotency_key) > 512:
                raise ValueError("idempotency key must be between 1 and 512 characters")
            token = hashlib.sha256(idempotency_key.encode()).hexdigest()
        else:
            token = str(uuid.uuid4())
        key = f"manual:{job_id}:{token}"
        return self._enqueue(job_id, key, now, "execution.triggered")

    def ingest_webhook(
        self, job_id: str, body: bytes, *, signature: str, idempotency_key: str
    ) -> ExecutionRecord:
        if len(body) > _JSON_LIMIT:
            raise ValueError("webhook body exceeds 1048576 bytes")
        if not idempotency_key or len(idempotency_key) > 512:
            raise ValueError("a bounded idempotency key is required")
        job = self.get_job(job_id)
        if job.trigger is not TriggerKind.WEBHOOK or not job.enabled:
            raise ValueError("job is not an enabled webhook")
        assert job.webhook_secret_ref is not None
        secret = self.secret_resolver(job.webhook_secret_ref).encode()
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise PermissionError("invalid webhook signature")
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError("webhook body must be a JSON object")
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        record = self._enqueue(job_id, f"webhook:{job_id}:{key_hash}", _now(), "webhook.accepted")
        # Payload remains the immutable job payload; inbound data is attached to the
        # execution record only through the action context assembled at dispatch.
        with self._transaction() as db:
            row = db.execute(
                "SELECT result FROM scheduler_executions WHERE id=?", (record.id,)
            ).fetchone()
            if row is not None and row["result"] is None:
                db.execute(
                    "UPDATE scheduler_executions SET result=? WHERE id=? AND status=?",
                    (_json({"webhook": decoded}), record.id, ExecutionStatus.PENDING),
                )
        return self.get_execution(record.id)

    def get_execution(self, execution_id: str) -> ExecutionRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM scheduler_executions WHERE id=?", (execution_id,)
            ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return self._execution(row)

    def list_executions(
        self, *, job_id: str | None = None, status: ExecutionStatus | None = None
    ) -> list[ExecutionRecord]:
        clauses: list[str] = []
        values: list[str] = []
        if job_id:
            clauses.append("job_id=?")
            values.append(job_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM scheduler_executions{where} ORDER BY created_at, id", values
            ).fetchall()
        return [self._execution(row) for row in rows]

    async def run_due(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[ExecutionRecord]:
        if not self.verify_audit():
            raise RuntimeError("scheduler audit integrity check failed")
        clock = (now or _now()).astimezone(UTC)
        self._recover_expired(clock)
        self._materialize_due(clock, limit)
        with self._connect() as db:
            rows = db.execute(
                """SELECT e.id FROM scheduler_executions e
                   JOIN scheduler_jobs j ON j.id=e.job_id
                   WHERE e.status=? AND e.next_attempt_at<=? AND j.enabled=1
                   ORDER BY e.next_attempt_at, e.created_at LIMIT ?""",
                (ExecutionStatus.PENDING, _iso(clock), limit),
            ).fetchall()
        claimed = [identifier for row in rows if (identifier := self._claim(row["id"], clock))]
        tasks = [asyncio.create_task(self._execute(identifier)) for identifier in claimed]
        for identifier, task in zip(claimed, tasks, strict=True):
            self._tasks[identifier] = task
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for identifier in claimed:
            self._tasks.pop(identifier, None)
        return [self.get_execution(identifier) for identifier in claimed]

    async def serve(self, *, poll_seconds: float = 1, stop: asyncio.Event | None = None) -> None:
        stopper = stop or asyncio.Event()
        while not stopper.is_set():
            await self.run_due()
            try:
                await asyncio.wait_for(stopper.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue

    async def cancel(self, execution_id: str) -> ExecutionRecord:
        task = self._tasks.get(execution_id)
        if task:
            task.cancel()
        now = _now()
        with self._transaction() as db:
            changed = db.execute(
                """UPDATE scheduler_executions SET status=?, lease_owner=NULL,
                   lease_until=NULL, updated_at=? WHERE id=? AND status IN (?,?)""",
                (
                    ExecutionStatus.CANCELLED,
                    _iso(now),
                    execution_id,
                    ExecutionStatus.PENDING,
                    ExecutionStatus.RUNNING,
                ),
            ).rowcount
            if changed:
                self._audit(db, "execution.cancelled", execution_id, {})
        if task:
            await asyncio.gather(task, return_exceptions=True)
        return self.get_execution(execution_id)

    def replay(self, execution_id: str) -> ExecutionRecord:
        original = self.get_execution(execution_id)
        if original.status not in {
            ExecutionStatus.DEAD,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.SUCCEEDED,
        }:
            raise ValueError("only terminal executions can be replayed")
        return self._enqueue(
            original.job_id,
            f"replay:{original.occurrence_key}:{uuid.uuid4()}",
            _now(),
            "execution.replayed",
        )

    def _enqueue(
        self, job_id: str, key: str, scheduled_for: datetime, audit: str
    ) -> ExecutionRecord:
        now = _now()
        identifier = str(uuid.uuid4())
        with self._transaction() as db:
            existing = db.execute(
                "SELECT id FROM scheduler_executions WHERE occurrence_key=?", (key,)
            ).fetchone()
            if existing:
                identifier = existing["id"]
            else:
                db.execute(
                    """INSERT INTO scheduler_executions
                       (id,job_id,occurrence_key,status,scheduled_for,next_attempt_at,attempts,
                        result,error,lease_owner,lease_until,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,?,?)""",
                    (
                        identifier,
                        job_id,
                        key,
                        ExecutionStatus.PENDING,
                        _iso(scheduled_for),
                        _iso(now),
                        _iso(now),
                        _iso(now),
                    ),
                )
                self._audit(db, audit, identifier, {"job_id": job_id})
        return self.get_execution(identifier)

    def _materialize_due(self, now: datetime, limit: int) -> None:
        with self._transaction() as db:
            rows = db.execute(
                """SELECT * FROM scheduler_jobs
                   WHERE enabled=1 AND next_run_at<=?
                   ORDER BY next_run_at LIMIT ?""",
                (_iso(now), limit),
            ).fetchall()
            for row in rows:
                spec = JobSpec.model_validate_json(row["spec"])
                due = _dt(row["next_run_at"])
                assert due is not None
                key = f"schedule:{row['id']}:{_iso(due)}"
                db.execute(
                    """INSERT OR IGNORE INTO scheduler_executions
                       (id,job_id,occurrence_key,status,scheduled_for,next_attempt_at,attempts,
                        result,error,lease_owner,lease_until,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,?,?)""",
                    (
                        str(uuid.uuid4()),
                        row["id"],
                        key,
                        ExecutionStatus.PENDING,
                        _iso(due),
                        _iso(now),
                        _iso(now),
                        _iso(now),
                    ),
                )
                upcoming = self._next_run(spec, due, now)
                # A consumed one-shot remains an enabled historical definition with
                # no next occurrence. Disabling it here would also prevent the
                # execution materialized in this transaction from being claimed.
                db.execute(
                    "UPDATE scheduler_jobs SET next_run_at=?,updated_at=? WHERE id=?",
                    (_iso(upcoming), _iso(now), row["id"]),
                )
                self._audit(db, "execution.scheduled", key, {"job_id": row["id"]})

    def _recover_expired(self, now: datetime) -> None:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id FROM scheduler_executions WHERE status=? AND lease_until<?",
                (ExecutionStatus.RUNNING, _iso(now)),
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE scheduler_executions SET status=?,next_attempt_at=?,lease_owner=NULL,
                       lease_until=NULL,updated_at=? WHERE id=?""",
                    (ExecutionStatus.PENDING, _iso(now), _iso(now), row["id"]),
                )
                self._audit(db, "execution.lease_expired", row["id"], {})

    def _claim(self, execution_id: str, now: datetime) -> str | None:
        with self._transaction() as db:
            row = db.execute(
                """SELECT e.*,j.spec FROM scheduler_executions e JOIN scheduler_jobs j
                   ON j.id=e.job_id WHERE e.id=?""",
                (execution_id,),
            ).fetchone()
            if row is None:
                return None
            spec = JobSpec.model_validate_json(row["spec"])
            active = db.execute(
                "SELECT count(*) FROM scheduler_executions WHERE job_id=? AND status=?",
                (row["job_id"], ExecutionStatus.RUNNING),
            ).fetchone()[0]
            if active >= spec.max_concurrency:
                return None
            lease_until = now + timedelta(
                seconds=min(300.0, max(self.lease_seconds, spec.timeout_seconds))
            )
            changed = db.execute(
                """UPDATE scheduler_executions SET status=?,attempts=attempts+1,
                   lease_owner=?,lease_until=?,updated_at=? WHERE id=? AND status=?""",
                (
                    ExecutionStatus.RUNNING,
                    self.owner,
                    _iso(lease_until),
                    _iso(now),
                    execution_id,
                    ExecutionStatus.PENDING,
                ),
            ).rowcount
            if not changed:
                return None
            self._audit(db, "execution.claimed", execution_id, {"owner": self.owner})
            return execution_id

    async def _execute(self, execution_id: str) -> None:
        with self._connect() as db:
            row = db.execute(
                """SELECT e.*,j.spec FROM scheduler_executions e JOIN scheduler_jobs j
                   ON j.id=e.job_id WHERE e.id=?""",
                (execution_id,),
            ).fetchone()
        if row is None:
            return
        spec = JobSpec.model_validate_json(row["spec"])
        action = self.actions.get(spec.action)
        try:
            if action is None:
                raise LookupError(f"action is not registered: {spec.action}")
            payload = dict(spec.payload)
            staged = json.loads(row["result"]) if row["result"] else None
            if isinstance(staged, dict) and "webhook" in staged:
                payload["webhook"] = staged["webhook"]
            context = ExecutionContext(
                execution_id=execution_id,
                job_id=row["job_id"],
                occurrence_key=row["occurrence_key"],
                attempt=row["attempts"],
                action=spec.action,
                payload=payload,
                scheduled_for=_required_dt(row["scheduled_for"]),
            )
            action_task = asyncio.create_task(
                asyncio.wait_for(action(context), timeout=spec.timeout_seconds)
            )
            heartbeat = asyncio.create_task(self._heartbeat(execution_id))
            done, _pending = await asyncio.wait(
                {action_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done and action_task not in done:
                # Another process cancelled or reclaimed the execution. Stop local
                # side effects instead of allowing a stale owner to finish them.
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)
                return
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            raw = await action_task
            result = raw if isinstance(raw, ActionResult) else ActionResult(output=dict(raw or {}))
            if not result.ok:
                raise RuntimeError(result.message or "action reported failure")
            self._finish(execution_id, result)
        except asyncio.CancelledError:
            # cancel() persists the terminal state; external process cancellation leaves
            # the lease to expire so another worker can safely recover the attempt.
            raise
        except Exception as exc:
            self._fail(execution_id, spec, exc)

    async def _heartbeat(self, execution_id: str) -> None:
        interval = max(1.0, min(5.0, self.lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            now = _now()
            with self._transaction() as db:
                changed = db.execute(
                    """UPDATE scheduler_executions SET lease_until=?,updated_at=?
                       WHERE id=? AND status=? AND lease_owner=?""",
                    (
                        _iso(now + timedelta(seconds=self.lease_seconds)),
                        _iso(now),
                        execution_id,
                        ExecutionStatus.RUNNING,
                        self.owner,
                    ),
                ).rowcount
                if not changed:
                    return

    def _finish(self, execution_id: str, result: ActionResult) -> None:
        now = _now()
        with self._transaction() as db:
            changed = db.execute(
                """UPDATE scheduler_executions SET status=?,result=?,error=NULL,
                   lease_owner=NULL,lease_until=NULL,updated_at=?
                   WHERE id=? AND status=? AND lease_owner=?""",
                (
                    ExecutionStatus.SUCCEEDED,
                    _json(result.model_dump(), limit=_OUTPUT_LIMIT),
                    _iso(now),
                    execution_id,
                    ExecutionStatus.RUNNING,
                    self.owner,
                ),
            ).rowcount
            if changed:
                self._audit(db, "execution.succeeded", execution_id, {})

    def _fail(self, execution_id: str, spec: JobSpec, error: Exception) -> None:
        now = _now()
        with self._transaction() as db:
            row = db.execute(
                """SELECT attempts FROM scheduler_executions
                   WHERE id=? AND status=? AND lease_owner=?""",
                (execution_id, ExecutionStatus.RUNNING, self.owner),
            ).fetchone()
            if row is None:
                return
            dead = row["attempts"] >= spec.max_attempts
            delay = spec.backoff_seconds * (2 ** max(0, row["attempts"] - 1))
            next_attempt = now if dead else now + timedelta(seconds=min(delay, 86_400))
            message = f"{type(error).__name__}: {error}"[:4096]
            db.execute(
                """UPDATE scheduler_executions SET status=?,error=?,next_attempt_at=?,
                   lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                (
                    ExecutionStatus.DEAD if dead else ExecutionStatus.PENDING,
                    message,
                    _iso(next_attempt),
                    _iso(now),
                    execution_id,
                ),
            )
            self._audit(
                db,
                "execution.dead" if dead else "execution.retry",
                execution_id,
                {"error": message},
            )

    def _first_run(self, spec: JobSpec, now: datetime) -> datetime | None:
        if spec.trigger is TriggerKind.ONCE:
            assert spec.run_at is not None
            return spec.run_at.astimezone(UTC)
        if spec.trigger is TriggerKind.INTERVAL:
            assert spec.interval_seconds is not None
            return now + timedelta(seconds=spec.interval_seconds)
        if spec.trigger is TriggerKind.CRON:
            assert spec.cron is not None
            local = now.astimezone(ZoneInfo(spec.timezone))
            return croniter(spec.cron, local).get_next(datetime).astimezone(UTC)
        return None

    def _next_run(self, spec: JobSpec, previous: datetime, now: datetime) -> datetime | None:
        if spec.trigger is TriggerKind.ONCE:
            return None
        if spec.trigger is TriggerKind.INTERVAL:
            assert spec.interval_seconds is not None
            candidate = previous + timedelta(seconds=spec.interval_seconds)
            while candidate <= now:
                candidate += timedelta(seconds=spec.interval_seconds)
            return candidate
        if spec.trigger is TriggerKind.CRON:
            assert spec.cron is not None
            local = now.astimezone(ZoneInfo(spec.timezone))
            return croniter(spec.cron, local).get_next(datetime).astimezone(UTC)
        return None

    def _job(self, row: sqlite3.Row) -> Job:
        spec = JobSpec.model_validate_json(row["spec"])
        values = spec.model_dump()
        values["enabled"] = bool(row["enabled"])
        return Job(
            **values,
            id=row["id"],
            created_at=_required_dt(row["created_at"]),
            updated_at=_required_dt(row["updated_at"]),
            next_run_at=_dt(row["next_run_at"]),
        )

    def _execution(self, row: sqlite3.Row) -> ExecutionRecord:
        result = json.loads(row["result"]) if row["result"] else None
        if isinstance(result, dict) and set(result) == {"webhook"}:
            result = None
        return ExecutionRecord(
            id=row["id"],
            job_id=row["job_id"],
            occurrence_key=row["occurrence_key"],
            status=row["status"],
            scheduled_for=_required_dt(row["scheduled_for"]),
            next_attempt_at=_required_dt(row["next_attempt_at"]),
            attempts=row["attempts"],
            result=result,
            error=row["error"],
            created_at=_required_dt(row["created_at"]),
            updated_at=_required_dt(row["updated_at"]),
        )

    def verify_audit(self) -> bool:
        """Return whether the append-only audit chain is internally consistent."""

        previous = "0" * 64
        with self._connect() as db:
            rows = db.execute("SELECT * FROM scheduler_audit ORDER BY seq").fetchall()
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            digest = hashlib.sha256(
                f"{previous}\0{row['at']}\0{row['kind']}\0{row['subject']}\0{row['detail']}".encode()
            ).hexdigest()
            if not hmac.compare_digest(digest, row["record_hash"]):
                return False
            previous = row["record_hash"]
        return True

    def _audit(
        self, db: sqlite3.Connection, kind: str, subject: str, detail: Mapping[str, Any]
    ) -> None:
        at = _iso(_now()) or ""
        encoded = _json(detail)
        prior = db.execute(
            "SELECT record_hash FROM scheduler_audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous_hash = prior[0] if prior else "0" * 64
        digest = hashlib.sha256(
            f"{previous_hash}\0{at}\0{kind}\0{subject}\0{encoded}".encode()
        ).hexdigest()
        db.execute(
            """INSERT INTO scheduler_audit
               (at,kind,subject,detail,previous_hash,record_hash) VALUES (?,?,?,?,?,?)""",
            (at, kind, subject, encoded, previous_hash, digest),
        )
