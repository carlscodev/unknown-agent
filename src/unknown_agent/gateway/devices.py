"""Cryptographically enrolled device delegation with durable leased commands."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict

DeviceState = Literal["active", "revoked"]
CommandState = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class DeviceError(RuntimeError):
    """Device authentication, authorization, or command failure."""


class Enrollment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    token: str
    expires_at: datetime


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str
    public_key: str
    capabilities: tuple[str, ...]
    state: DeviceState
    created_at: datetime
    last_seen_at: datetime | None = None


class DeviceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    device_id: str
    capability: str
    payload: dict[str, Any]
    state: CommandState
    attempts: int
    max_attempts: int
    timeout_seconds: int
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: datetime
    updated_at: datetime


class DeviceController:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS device_enrollments(
                  token_hash TEXT PRIMARY KEY,salt TEXT NOT NULL,expires_at TEXT NOT NULL,
                  attempts INTEGER NOT NULL,consumed_at TEXT,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(
                  id TEXT PRIMARY KEY,name TEXT NOT NULL,public_key TEXT NOT NULL,
                  capabilities TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,last_seen_at TEXT);
                CREATE TABLE IF NOT EXISTS device_nonces(
                  device_id TEXT NOT NULL,nonce TEXT NOT NULL,seen_at TEXT NOT NULL,
                  PRIMARY KEY(device_id,nonce));
                CREATE TABLE IF NOT EXISTS device_commands(
                  id TEXT PRIMARY KEY,device_id TEXT NOT NULL,capability TEXT NOT NULL,
                  payload TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL,
                  max_attempts INTEGER NOT NULL,timeout_seconds INTEGER NOT NULL,
                  lease_token TEXT,lease_expires_at TEXT,result TEXT,error TEXT NOT NULL,
                  idempotency_key TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                  UNIQUE(device_id,idempotency_key));
                CREATE INDEX IF NOT EXISTS device_commands_claim
                  ON device_commands(device_id,state,created_at);
                """
            )

    def issue_enrollment(self, *, ttl_seconds: int = 300) -> Enrollment:
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("enrollment TTL must be between 1 and 300 seconds")
        token = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)
        with self._connect() as db:
            db.execute(
                "INSERT INTO device_enrollments VALUES(?,?,?,0,NULL,?)",
                (self._hash(token, salt), salt, expires.isoformat(), now.isoformat()),
            )
        return Enrollment(token=token, expires_at=expires)

    def enroll(
        self,
        token: str,
        *,
        device_id: str,
        name: str,
        public_key: str,
        capabilities: list[str],
        signature: str,
    ) -> Device:
        self._validate_device_id(device_id)
        normalized = self._capabilities(capabilities)
        key = self._public_key(public_key)
        proof = self.enrollment_proof(token, device_id, public_key, normalized)
        try:
            key.verify(bytes.fromhex(signature), proof)
        except (InvalidSignature, ValueError) as exc:
            raise DeviceError("invalid enrollment signature") from exc
        now = datetime.now(UTC)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM device_enrollments WHERE consumed_at IS NULL
                AND expires_at>=? AND attempts<5 ORDER BY created_at DESC""",
                (now.isoformat(),),
            ).fetchall()
            matched: sqlite3.Row | None = None
            for row in rows:
                if secrets.compare_digest(self._hash(token, row["salt"]), row["token_hash"]):
                    matched = row
                    break
            if matched is None:
                raise DeviceError("invalid or expired enrollment token")
            existing = db.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone()
            if existing is not None:
                raise DeviceError("device id is already registered")
            stamp = now.isoformat()
            db.execute(
                """INSERT INTO devices VALUES(?,?,?,?,?,?,?,NULL)""",
                (
                    device_id,
                    name[:256],
                    public_key.lower(),
                    json.dumps(normalized),
                    "active",
                    stamp,
                    stamp,
                ),
            )
            db.execute(
                "UPDATE device_enrollments SET consumed_at=? WHERE token_hash=?",
                (stamp, matched["token_hash"]),
            )
        return self.get_device(device_id)

    @staticmethod
    def enrollment_proof(
        token: str, device_id: str, public_key: str, capabilities: list[str] | tuple[str, ...]
    ) -> bytes:
        canonical = json.dumps(sorted(capabilities), separators=(",", ":"))
        message = (
            f"unknown-agent-device-enroll:v1:{token}:{device_id}:{public_key.lower()}:{canonical}"
        )
        return message.encode()

    def verify_request(
        self,
        device_id: str,
        *,
        timestamp: int,
        nonce: str,
        body: bytes,
        signature: str,
    ) -> Device:
        now = datetime.now(UTC)
        if abs(int(now.timestamp()) - timestamp) > 300:
            raise DeviceError("device request timestamp outside replay window")
        if not 16 <= len(nonce) <= 128:
            raise DeviceError("invalid device nonce")
        device = self.get_device(device_id)
        if device.state != "active":
            raise DeviceError("device is revoked")
        digest = hashlib.sha256(body).hexdigest()
        message = f"unknown-agent-device-request:v1:{timestamp}:{nonce}:{digest}".encode()
        try:
            self._public_key(device.public_key).verify(bytes.fromhex(signature), message)
        except (InvalidSignature, ValueError) as exc:
            raise DeviceError("invalid device request signature") from exc
        with self._connect() as db:
            db.execute(
                "DELETE FROM device_nonces WHERE seen_at<?",
                ((now - timedelta(seconds=300)).isoformat(),),
            )
            try:
                db.execute(
                    "INSERT INTO device_nonces VALUES(?,?,?)", (device_id, nonce, now.isoformat())
                )
            except sqlite3.IntegrityError as exc:
                raise DeviceError("replayed device request") from exc
            db.execute(
                "UPDATE devices SET last_seen_at=?,updated_at=? WHERE id=?",
                (now.isoformat(), now.isoformat(), device_id),
            )
        return self.get_device(device_id)

    @staticmethod
    def request_proof(timestamp: int, nonce: str, body: bytes) -> bytes:
        digest = hashlib.sha256(body).hexdigest()
        return f"unknown-agent-device-request:v1:{timestamp}:{nonce}:{digest}".encode()

    def enqueue(
        self,
        device_id: str,
        capability: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: int = 60,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
    ) -> DeviceCommand:
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("command timeout must be between 1 and 300 seconds")
        if not 1 <= max_attempts <= 5:
            raise ValueError("command attempts must be between 1 and 5")
        device = self.get_device(device_id)
        if device.state != "active" or capability not in device.capabilities:
            raise DeviceError("device lacks the requested capability")
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > 1_048_576:
            raise DeviceError("device command payload exceeds 1 MiB")
        identifier = str(uuid.uuid4())
        stamp = datetime.now(UTC).isoformat()
        with self._connect() as db:
            try:
                db.execute(
                    """INSERT INTO device_commands
                    VALUES(?,?,?,?,?,0,?,?,NULL,NULL,NULL,'',?,?,?)""",
                    (
                        identifier,
                        device_id,
                        capability,
                        encoded,
                        "pending",
                        max_attempts,
                        timeout_seconds,
                        idempotency_key,
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                row = db.execute(
                    "SELECT id FROM device_commands WHERE device_id=? AND idempotency_key=?",
                    (device_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise
                identifier = row["id"]
        return self.get_command(identifier)

    def claim(self, device_id: str, *, lease_seconds: int = 60) -> DeviceCommand | None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("lease must be between 1 and 300 seconds")
        device = self.get_device(device_id)
        if device.state != "active":
            raise DeviceError("device is revoked")
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(24)
        expiry = now + timedelta(seconds=lease_seconds)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id FROM device_commands WHERE device_id=? AND attempts<max_attempts
                AND (state='pending' OR (state='running' AND lease_expires_at<?))
                ORDER BY created_at LIMIT 1""",
                (device_id, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE device_commands SET state='running',attempts=attempts+1,
                lease_token=?,lease_expires_at=?,updated_at=? WHERE id=?""",
                (token, expiry.isoformat(), now.isoformat(), row["id"]),
            )
        return self.get_command(row["id"])

    def complete(
        self,
        command_id: str,
        lease_token: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> DeviceCommand:
        encoded = json.dumps(result or {}, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > 1_048_576 or len(error) > 10_000:
            raise DeviceError("device command result exceeds limits")
        now = datetime.now(UTC)
        state = "failed" if error else "succeeded"
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE device_commands SET state=?,result=?,error=?,lease_token=NULL,
                lease_expires_at=NULL,updated_at=? WHERE id=? AND state='running'
                AND lease_token=? AND lease_expires_at>=?""",
                (state, encoded, error, now.isoformat(), command_id, lease_token, now.isoformat()),
            )
            if cursor.rowcount != 1:
                raise DeviceError("invalid or expired command lease")
        return self.get_command(command_id)

    def revoke(self, device_id: str) -> Device:
        stamp = datetime.now(UTC).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE devices SET state='revoked',updated_at=? WHERE id=?", (stamp, device_id)
            )
            if cursor.rowcount != 1:
                raise DeviceError("device not found")
            db.execute(
                """UPDATE device_commands SET state='cancelled',updated_at=?
                WHERE device_id=? AND state IN ('pending','running')""",
                (stamp, device_id),
            )
        return self.get_device(device_id)

    def get_device(self, device_id: str) -> Device:
        with self._connect() as db:
            row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise DeviceError("device not found")
        return Device(
            id=row["id"],
            name=row["name"],
            public_key=row["public_key"],
            capabilities=tuple(json.loads(row["capabilities"])),
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"])
            if row["last_seen_at"]
            else None,
        )

    def get_command(self, command_id: str) -> DeviceCommand:
        with self._connect() as db:
            row = db.execute("SELECT * FROM device_commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise DeviceError("device command not found")
        return DeviceCommand(
            id=row["id"],
            device_id=row["device_id"],
            capability=row["capability"],
            payload=json.loads(row["payload"]),
            state=row["state"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            timeout_seconds=row["timeout_seconds"],
            lease_token=row["lease_token"],
            lease_expires_at=(
                datetime.fromisoformat(row["lease_expires_at"]) if row["lease_expires_at"] else None
            ),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _capabilities(capabilities: list[str]) -> list[str]:
        normalized = sorted(set(capabilities))
        if not normalized or len(normalized) > 64:
            raise ValueError("devices require between 1 and 64 capabilities")
        for capability in normalized:
            pieces = capability.split(".")
            if not 2 <= len(pieces) <= 4 or any(
                not piece or not piece.replace("_", "").replace("-", "").isalnum()
                for piece in pieces
            ):
                raise ValueError(f"invalid device capability: {capability}")
        return normalized

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        try:
            uuid.UUID(device_id)
        except ValueError as exc:
            raise ValueError("device id must be a UUID") from exc

    @staticmethod
    def _public_key(value: str) -> Ed25519PublicKey:
        try:
            raw = bytes.fromhex(value)
            if len(raw) != 32:
                raise ValueError
            return Ed25519PublicKey.from_public_bytes(raw)
        except ValueError as exc:
            raise ValueError("public key must be 32-byte Ed25519 hex") from exc

    @staticmethod
    def _hash(token: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{token}".encode()).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db
