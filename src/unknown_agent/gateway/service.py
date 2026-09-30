"""Durable channel-neutral inbox/outbox gateway."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from unknown_agent.config.secrets import redact_text
from unknown_agent.gateway.contracts import (
    GatewayRecord,
    InboundEnvelope,
    OutboundDraft,
    OutboundEnvelope,
)

MAX_GATEWAY_TIMEOUT_SECONDS = 300.0
RouteHandler = Callable[[InboundEnvelope], Awaitable[list[OutboundDraft]]]
Transport = Callable[[OutboundEnvelope], Awaitable[Mapping[str, Any] | None]]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _safe_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(raw.encode()) > 1_048_576:
        raise ValueError("gateway envelope exceeds 1048576 bytes")
    return redact_text(raw)


class Gateway:
    """SQLite/WAL gateway with deduplication, leases, retries, and an outbox."""

    def __init__(self, path: Path, *, owner: str | None = None, access: Any = None) -> None:
        self.path = path
        self.owner = owner or f"gateway-{uuid.uuid4()}"
        self.access = access
        self.routes: dict[str, RouteHandler] = {}
        self.transports: dict[str, Transport] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS gateway_inbox(
                  id TEXT PRIMARY KEY, channel TEXT NOT NULL, external_id TEXT NOT NULL,
                  envelope_json TEXT NOT NULL, envelope_hash TEXT NOT NULL,
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT NOT NULL, lease_owner TEXT, lease_until TEXT,
                  error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(channel,external_id)
                );
                CREATE INDEX IF NOT EXISTS gateway_inbox_due
                  ON gateway_inbox(status,next_attempt_at,lease_until);
                CREATE TABLE IF NOT EXISTS gateway_outbox(
                  id TEXT PRIMARY KEY, inbox_id TEXT REFERENCES gateway_inbox(id),
                  channel TEXT NOT NULL, envelope_json TEXT NOT NULL, envelope_hash TEXT NOT NULL,
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT NOT NULL, lease_owner TEXT, lease_until TEXT,
                  result_json TEXT, error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS gateway_outbox_due
                  ON gateway_outbox(status,next_attempt_at,lease_until);
                CREATE TABLE IF NOT EXISTS gateway_registry(
                  kind TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL,
                  registered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                  PRIMARY KEY(kind,name)
                );
                """
            )

    def register_route(
        self, channel: str, handler: RouteHandler, *, source: str = "application"
    ) -> None:
        self._register("route", channel, handler, source)
        self.routes[channel] = handler

    def register_transport(
        self, channel: str, transport: Transport, *, source: str = "application"
    ) -> None:
        self._register("transport", channel, transport, source)
        self.transports[channel] = transport

    def _register(self, kind: str, name: str, handler: Any, source: str) -> None:
        if not name or not inspect.iscoroutinefunction(handler):
            raise TypeError(f"gateway {kind} must be a named async function")
        target = self.routes if kind == "route" else self.transports
        if name in target:
            raise ValueError(f"gateway {kind} already registered: {name}")
        now = _iso(_now())
        with self._transaction() as db:
            db.execute(
                """INSERT INTO gateway_registry VALUES(?,?,?,?,?)
                   ON CONFLICT(kind,name) DO UPDATE SET
                   source=excluded.source,last_seen_at=excluded.last_seen_at""",
                (kind, name, source, now, now),
            )

    def receive(self, envelope: InboundEnvelope) -> str:
        if self.access is not None:
            self.access.authorize(
                envelope.source.channel,
                envelope.source.account,
                envelope.sender_id,
                "agent.run",
            )
        encoded = _safe_json(envelope.model_dump(mode="json"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        identifier = str(uuid.uuid4())
        now = _iso(_now())
        with self._transaction() as db:
            existing = db.execute(
                "SELECT id,envelope_hash FROM gateway_inbox WHERE channel=? AND external_id=?",
                (envelope.source.channel, envelope.external_id),
            ).fetchone()
            if existing:
                if existing["envelope_hash"] != digest:
                    raise ValueError("duplicate external id has different content")
                return str(existing["id"])
            db.execute(
                """INSERT INTO gateway_inbox VALUES(?,?,?,?,?,'pending',0,?,NULL,NULL,'',?,?)""",
                (
                    identifier,
                    envelope.source.channel,
                    envelope.external_id,
                    encoded,
                    digest,
                    now,
                    now,
                    now,
                ),
            )
        return identifier

    async def drain(self, *, limit: int = 100) -> list[GatewayRecord]:
        now = _now()
        self._recover(now)
        inbox_ids = self._due("gateway_inbox", now, limit)
        for identifier in inbox_ids:
            if self._claim("gateway_inbox", identifier, now):
                await self._route(identifier)
        send_clock = _now()
        outbox_ids = self._due("gateway_outbox", send_clock, limit)
        for identifier in outbox_ids:
            if self._claim("gateway_outbox", identifier, send_clock):
                await self._send(identifier)
        return self.records(limit=limit)

    async def serve(self, *, poll_seconds: float = 1, stop: asyncio.Event | None = None) -> None:
        if not 0.1 <= poll_seconds <= 300:
            raise ValueError("gateway poll interval must be between 0.1 and 300 seconds")
        stopper = stop or asyncio.Event()
        while not stopper.is_set():
            await self.drain()
            try:
                await asyncio.wait_for(stopper.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue

    def _recover(self, now: datetime) -> None:
        with self._transaction() as db:
            for table in ("gateway_inbox", "gateway_outbox"):
                db.execute(
                    f"""UPDATE {table} SET status='pending',next_attempt_at=?,lease_owner=NULL,
                        lease_until=NULL,updated_at=? WHERE status='running' AND lease_until<?""",
                    (_iso(now), _iso(now), _iso(now)),
                )

    def _due(self, table: str, now: datetime, limit: int) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT id FROM {table} WHERE status='pending' AND next_attempt_at<=?
                    ORDER BY created_at,id LIMIT ?""",
                (_iso(now), limit),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def _claim(self, table: str, identifier: str, now: datetime) -> bool:
        with self._transaction() as db:
            changed = db.execute(
                f"""UPDATE {table} SET status='running',attempts=attempts+1,
                    lease_owner=?,lease_until=?,updated_at=? WHERE id=? AND status='pending'""",
                (self.owner, _iso(now + timedelta(seconds=300)), _iso(now), identifier),
            ).rowcount
        return bool(changed)

    async def _route(self, identifier: str) -> None:
        row = self._row("gateway_inbox", identifier)
        try:
            self._verify(row)
            envelope = InboundEnvelope.model_validate_json(row["envelope_json"])
            handler = self.routes.get(envelope.source.channel) or self.routes.get("*")
            if handler is None:
                raise LookupError(f"no route for channel: {envelope.source.channel}")
            drafts = await asyncio.wait_for(handler(envelope), timeout=300)
            now = _iso(_now())
            with self._transaction() as db:
                for draft in drafts:
                    outbound = OutboundEnvelope(id=str(uuid.uuid4()), **draft.model_dump())
                    encoded = _safe_json(outbound.model_dump(mode="json"))
                    db.execute(
                        """INSERT INTO gateway_outbox VALUES(?,?,?,?,?,'pending',0,?,NULL,NULL,
                           NULL,'',?,?)""",
                        (
                            outbound.id,
                            identifier,
                            outbound.destination.channel,
                            encoded,
                            hashlib.sha256(encoded.encode()).hexdigest(),
                            now,
                            now,
                            now,
                        ),
                    )
                db.execute(
                    """UPDATE gateway_inbox SET status='succeeded',lease_owner=NULL,
                       lease_until=NULL,updated_at=? WHERE id=? AND lease_owner=?""",
                    (now, identifier, self.owner),
                )
        except Exception as exc:
            self._fail("gateway_inbox", identifier, exc)

    async def _send(self, identifier: str) -> None:
        row = self._row("gateway_outbox", identifier)
        try:
            self._verify(row)
            envelope = OutboundEnvelope.model_validate_json(row["envelope_json"])
            transport = self.transports.get(envelope.destination.channel)
            if transport is None:
                raise LookupError(f"no transport for channel: {envelope.destination.channel}")
            result = await asyncio.wait_for(transport(envelope), timeout=300)
            now = _iso(_now())
            with self._transaction() as db:
                db.execute(
                    """UPDATE gateway_outbox SET status='succeeded',result_json=?,error='',
                       lease_owner=NULL,lease_until=NULL,updated_at=?
                       WHERE id=? AND lease_owner=?""",
                    (_safe_json(dict(result or {})), now, identifier, self.owner),
                )
        except Exception as exc:
            self._fail("gateway_outbox", identifier, exc)

    def _fail(self, table: str, identifier: str, error: Exception) -> None:
        row = self._row(table, identifier)
        attempts = int(row["attempts"])
        dead = attempts >= 5
        delay = min(300, 2 ** max(0, attempts - 1))
        now = _now()
        with self._transaction() as db:
            db.execute(
                f"""UPDATE {table} SET status=?,next_attempt_at=?,error=?,lease_owner=NULL,
                    lease_until=NULL,updated_at=? WHERE id=? AND lease_owner=?""",
                (
                    "dead" if dead else "pending",
                    _iso(now + timedelta(seconds=delay)),
                    f"{type(error).__name__}: {error}"[:4096],
                    _iso(now),
                    identifier,
                    self.owner,
                ),
            )

    def _row(self, table: str, identifier: str) -> sqlite3.Row:
        with self._connect() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return row

    @staticmethod
    def _verify(row: sqlite3.Row) -> None:
        encoded = row["envelope_json"]
        if hashlib.sha256(encoded.encode()).hexdigest() != row["envelope_hash"]:
            raise RuntimeError("gateway envelope integrity check failed")

    def records(self, *, limit: int = 100) -> list[GatewayRecord]:
        result: list[GatewayRecord] = []
        with self._connect() as db:
            for kind, table in (("inbox", "gateway_inbox"), ("outbox", "gateway_outbox")):
                rows = db.execute(
                    f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
                result.extend(
                    GatewayRecord(
                        id=row["id"],
                        kind=cast(Literal["inbox", "outbox"], kind),
                        channel=row["channel"],
                        status=row["status"],
                        attempts=row["attempts"],
                        created_at=_dt(row["created_at"]),
                        updated_at=_dt(row["updated_at"]),
                        error=row["error"],
                    )
                    for row in rows
                )
        return sorted(result, key=lambda item: item.created_at, reverse=True)
