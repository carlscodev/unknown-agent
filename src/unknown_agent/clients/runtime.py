"""Shared encrypted, offline-first runtime for desktop and mobile clients."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unknown_agent.config.secrets import is_reference, resolve_secret

EventKind = Literal["message", "approval", "command", "status"]
OperationKind = Literal["message.send", "approval.resolve", "command.result", "receipt"]
_MAX_PAYLOAD = 1_048_576


class ClientError(RuntimeError):
    """Client storage, transport, or synchronization failure."""


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=256)
    sequence: int = Field(gt=0)
    kind: EventKind
    payload: dict[str, Any]
    created_at: datetime


class ClientOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=256)
    kind: OperationKind
    payload: dict[str, Any]
    created_at: datetime


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cursor: str = Field(default="", max_length=512)
    operations: tuple[ClientOperation, ...] = Field(default=(), max_length=100)


class SyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cursor: str = Field(max_length=512)
    events: tuple[ClientEvent, ...] = Field(default=(), max_length=1000)
    acknowledged_operation_ids: tuple[str, ...] = Field(default=(), max_length=100)


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str = Field(pattern=r"^https://")
    device_id: str = Field(min_length=1, max_length=64)
    signing_key_ref: str
    storage_key_ref: str
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_retries: int = Field(default=3, ge=0, le=5)

    @field_validator("signing_key_ref", "storage_key_ref")
    @classmethod
    def references_only(cls, value: str) -> str:
        if not is_reference(value):
            raise ValueError("client keys must use secret references")
        return value


class SecureClientStore:
    """SQLite metadata with ChaCha20-Poly1305 encrypted event and operation payloads."""

    def __init__(self, database: Path, key_ref: str) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = ChaCha20Poly1305(_key(key_ref, "storage"))
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS client_metadata(
                  key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS client_events(
                  id TEXT PRIMARY KEY,sequence INTEGER NOT NULL UNIQUE,kind TEXT NOT NULL,
                  ciphertext BLOB NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS client_operations(
                  id TEXT PRIMARY KEY,kind TEXT NOT NULL,ciphertext BLOB NOT NULL,
                  state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS client_operations_pending
                  ON client_operations(state,created_at);
                """
            )

    @property
    def cursor(self) -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM client_metadata WHERE key='cursor'").fetchone()
        return str(row["value"]) if row else ""

    def queue(
        self, kind: OperationKind, payload: dict[str, Any], *, operation_id: str | None = None
    ) -> ClientOperation:
        operation = ClientOperation(
            id=operation_id or str(uuid.uuid4()),
            kind=kind,
            payload=payload,
            created_at=datetime.now(UTC),
        )
        plaintext = _payload_bytes(payload)
        ciphertext = self._encrypt("operation", operation.id, plaintext)
        stamp = operation.created_at.isoformat()
        with self._connect() as db:
            try:
                db.execute(
                    "INSERT INTO client_operations VALUES(?,?,?,'pending',?,?)",
                    (operation.id, operation.kind, ciphertext, stamp, stamp),
                )
            except sqlite3.IntegrityError:
                existing = self.get_operation(operation.id)
                if existing.kind != kind or existing.payload != payload:
                    raise ClientError("operation id was reused with different content") from None
                return existing
        return operation

    def resolve_approval(
        self, approval_id: str, *, approved: bool, reason: str = ""
    ) -> ClientOperation:
        return self.queue(
            "approval.resolve",
            {"approval_id": approval_id, "approved": approved, "reason": reason[:2000]},
            operation_id=f"approval:{approval_id}",
        )

    def pending(self, *, limit: int = 100) -> tuple[ClientOperation, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("pending operation limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM client_operations WHERE state='pending'
                ORDER BY created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(self._operation(row) for row in rows)

    def get_operation(self, operation_id: str) -> ClientOperation:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM client_operations WHERE id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise ClientError("client operation not found")
        return self._operation(row)

    def apply(self, response: SyncResponse) -> None:
        event_ids = [event.id for event in response.events]
        sequences = [event.sequence for event in response.events]
        if len(event_ids) != len(set(event_ids)) or len(sequences) != len(set(sequences)):
            raise ClientError("sync response contains duplicate events")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for event in response.events:
                ciphertext = self._encrypt("event", event.id, _payload_bytes(event.payload))
                try:
                    db.execute(
                        "INSERT INTO client_events VALUES(?,?,?,?,?)",
                        (
                            event.id,
                            event.sequence,
                            event.kind,
                            ciphertext,
                            event.created_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    row = db.execute(
                        "SELECT id,sequence FROM client_events WHERE id=? OR sequence=?",
                        (event.id, event.sequence),
                    ).fetchone()
                    if row is None or row["id"] != event.id or row["sequence"] != event.sequence:
                        raise ClientError("event identity conflicts with local history") from None
            for operation_id in response.acknowledged_operation_ids:
                cursor = db.execute(
                    """UPDATE client_operations SET state='acknowledged',updated_at=?
                    WHERE id=? AND state='pending'""",
                    (datetime.now(UTC).isoformat(), operation_id),
                )
                if cursor.rowcount == 0:
                    row = db.execute(
                        "SELECT state FROM client_operations WHERE id=?", (operation_id,)
                    ).fetchone()
                    if row is None:
                        raise ClientError("server acknowledged an unknown operation")
            db.execute(
                """INSERT INTO client_metadata VALUES('cursor',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (response.cursor,),
            )

    def events(self, *, after_sequence: int = 0, limit: int = 100) -> tuple[ClientEvent, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("event limit must be between 1 and 1000")
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM client_events WHERE sequence>? ORDER BY sequence LIMIT ?""",
                (after_sequence, limit),
            ).fetchall()
        return tuple(
            ClientEvent(
                id=row["id"],
                sequence=row["sequence"],
                kind=row["kind"],
                payload=self._decrypt("event", row["id"], row["ciphertext"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    def _operation(self, row: sqlite3.Row) -> ClientOperation:
        return ClientOperation(
            id=row["id"],
            kind=row["kind"],
            payload=self._decrypt("operation", row["id"], row["ciphertext"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _encrypt(self, domain: str, identifier: str, plaintext: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        aad = f"unknown-agent-client:v1:{domain}:{identifier}".encode()
        return nonce + self._cipher.encrypt(nonce, plaintext, aad)

    def _decrypt(self, domain: str, identifier: str, value: bytes) -> dict[str, Any]:
        try:
            nonce, ciphertext = value[:12], value[12:]
            aad = f"unknown-agent-client:v1:{domain}:{identifier}".encode()
            payload = json.loads(self._cipher.decrypt(nonce, ciphertext, aad))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except Exception as exc:
            raise ClientError("encrypted client payload failed authentication") from exc

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database, timeout=30)
        database.row_factory = sqlite3.Row
        return database


class ClientTransport:
    def __init__(self, config: ClientConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._private = Ed25519PrivateKey.from_private_bytes(
            _key(config.signing_key_ref, "signing")
        )

    async def sync(self, request: SyncRequest) -> SyncResponse:
        body = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            timestamp = int(time.time())
            nonce = secrets.token_urlsafe(24)
            proof = _request_proof(timestamp, nonce, body)
            try:
                async with self.client.stream(
                    "POST",
                    f"{self.config.endpoint.rstrip('/')}/v1/client/sync",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Device-Id": self.config.device_id,
                        "X-Device-Timestamp": str(timestamp),
                        "X-Device-Nonce": nonce,
                        "X-Device-Signature": self._private.sign(proof).hex(),
                    },
                ) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_PAYLOAD:
                            raise ClientError("sync response exceeds 1 MiB")
                return SyncResponse.model_validate_json(bytes(content))
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                    and exc.response.status_code != 429
                ):
                    break
                await asyncio.sleep(min(8, 0.25 * (2**attempt)))
        raise ClientError(f"client sync failed: {type(last_error).__name__}") from last_error


class ClientRuntime:
    def __init__(self, store: SecureClientStore, transport: ClientTransport) -> None:
        self.store = store
        self.transport = transport

    async def sync_once(self) -> SyncResponse:
        request = SyncRequest(cursor=self.store.cursor, operations=self.store.pending())
        response = await self.transport.sync(request)
        self.store.apply(response)
        return response


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_PAYLOAD:
        raise ClientError("client payload exceeds 1 MiB")
    return encoded


def _key(reference: str, purpose: str) -> bytes:
    value = resolve_secret(reference)
    if not value:
        raise ClientError(f"client {purpose} key did not resolve")
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ClientError(f"invalid client {purpose} key") from exc
    if len(raw) != 32:
        raise ClientError(f"client {purpose} key must contain 32 bytes")
    return raw


def _request_proof(timestamp: int, nonce: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"unknown-agent-device-request:v1:{timestamp}:{nonce}:{digest}".encode()
