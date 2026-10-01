"""Durable server-side synchronization ledger for installed clients."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unknown_agent.clients.runtime import (
    ClientError,
    ClientEvent,
    EventKind,
    SyncRequest,
    SyncResponse,
)


class ClientSyncService:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS client_sync_events(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
                  target_device TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS client_sync_operations(
                  device_id TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,
                  payload TEXT NOT NULL,payload_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,
                  PRIMARY KEY(device_id,id));
                CREATE TABLE IF NOT EXISTS client_approval_decisions(
                  approval_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,approved INTEGER NOT NULL,
                  reason TEXT NOT NULL,operation_id TEXT NOT NULL,created_at TEXT NOT NULL);
                """
            )

    def publish(
        self, kind: EventKind, payload: dict[str, Any], *, target_device: str | None = None
    ) -> ClientEvent:
        encoded = _encoded(payload)
        identifier = str(uuid.uuid4())
        stamp = datetime.now(UTC)
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO client_sync_events
                (id,target_device,kind,payload,created_at) VALUES(?,?,?,?,?)""",
                (identifier, target_device, kind, encoded, stamp.isoformat()),
            )
            if cursor.lastrowid is None:
                raise ClientError("event sequence was not allocated")
            sequence = cursor.lastrowid
        return ClientEvent(
            id=identifier,
            sequence=sequence,
            kind=kind,
            payload=payload,
            created_at=stamp,
        )

    def sync(
        self, device_id: str, capabilities: tuple[str, ...], request: SyncRequest
    ) -> SyncResponse:
        try:
            cursor = int(request.cursor or "0")
        except ValueError as exc:
            raise ClientError("invalid synchronization cursor") from exc
        if cursor < 0:
            raise ClientError("invalid synchronization cursor")
        acknowledged: list[str] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for operation in request.operations:
                if operation.kind not in capabilities:
                    raise ClientError(f"device lacks capability {operation.kind}")
                payload = _encoded(operation.payload)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                existing = db.execute(
                    """SELECT payload_sha256 FROM client_sync_operations
                    WHERE device_id=? AND id=?""",
                    (device_id, operation.id),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != digest:
                        raise ClientError("operation id conflicts with synchronized content")
                    acknowledged.append(operation.id)
                    continue
                db.execute(
                    "INSERT INTO client_sync_operations VALUES(?,?,?,?,?,?)",
                    (
                        device_id,
                        operation.id,
                        operation.kind,
                        payload,
                        digest,
                        operation.created_at.isoformat(),
                    ),
                )
                if operation.kind == "approval.resolve":
                    self._record_approval(db, device_id, operation.id, operation.payload)
                acknowledged.append(operation.id)
            rows = db.execute(
                """SELECT * FROM client_sync_events WHERE sequence>?
                AND (target_device IS NULL OR target_device=?) ORDER BY sequence LIMIT 1000""",
                (cursor, device_id),
            ).fetchall()
            events = tuple(
                ClientEvent(
                    id=row["id"],
                    sequence=row["sequence"],
                    kind=row["kind"],
                    payload=json.loads(row["payload"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            )
            next_cursor = str(events[-1].sequence if events else cursor)
        return SyncResponse(
            cursor=next_cursor,
            events=events,
            acknowledged_operation_ids=tuple(acknowledged),
        )

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM client_approval_decisions WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _record_approval(
        db: sqlite3.Connection,
        device_id: str,
        operation_id: str,
        payload: dict[str, Any],
    ) -> None:
        approval_id = payload.get("approval_id")
        approved = payload.get("approved")
        reason = payload.get("reason", "")
        if not isinstance(approval_id, str) or not approval_id or not isinstance(approved, bool):
            raise ClientError("invalid approval decision")
        try:
            db.execute(
                "INSERT INTO client_approval_decisions VALUES(?,?,?,?,?,?)",
                (
                    approval_id,
                    device_id,
                    int(approved),
                    str(reason)[:2000],
                    operation_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            existing = db.execute(
                "SELECT approved,device_id FROM client_approval_decisions WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if existing is None or bool(existing["approved"]) != approved:
                raise ClientError("approval already has a conflicting decision") from None

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database, timeout=30)
        database.row_factory = sqlite3.Row
        return database


def _encoded(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 1_048_576:
        raise ClientError("client sync payload exceeds 1 MiB")
    return encoded
