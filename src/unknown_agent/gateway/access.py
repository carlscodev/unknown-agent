"""Durable channel identity, ACL, and one-time pairing."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["viewer", "operator", "owner"]
_PERMISSIONS = {
    "viewer": frozenset({"status.read"}),
    "operator": frozenset({"status.read", "message.send", "agent.run"}),
    "owner": frozenset(
        {"status.read", "message.send", "agent.run", "acl.manage", "device.delegate"}
    ),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    channel: str
    account: str
    sender_id: str
    role: Role
    enabled: bool
    created_at: datetime


class PairingCode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    code: str
    channel: str
    account: str
    role: Role
    expires_at: datetime


class AccessDeniedError(PermissionError):
    pass


class AccessController:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init(self) -> None:
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS gateway_identities(
              id TEXT PRIMARY KEY,channel TEXT NOT NULL,account TEXT NOT NULL,
              sender_id TEXT NOT NULL,
              role TEXT NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(channel,account,sender_id));
            CREATE TABLE IF NOT EXISTS gateway_pairings(
              id TEXT PRIMARY KEY,channel TEXT NOT NULL,account TEXT NOT NULL,role TEXT NOT NULL,
              salt TEXT NOT NULL,code_hash TEXT NOT NULL,expires_at TEXT NOT NULL,
              attempts INTEGER NOT NULL,
              consumed_at TEXT,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS gateway_pairings_active
              ON gateway_pairings(expires_at,consumed_at);
            """)

    def issue(
        self, channel: str, account: str, *, role: Role = "operator", ttl_seconds: int = 300
    ) -> PairingCode:
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("pairing TTL must be between 1 and 300 seconds")
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        now = _now()
        identifier = str(uuid.uuid4())
        digest = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()
        expires = now + timedelta(seconds=ttl_seconds)
        with self._db() as db:
            db.execute(
                "INSERT INTO gateway_pairings VALUES(?,?,?,?,?,?,?,0,NULL,?)",
                (identifier, channel, account, role, salt, digest, _iso(expires), _iso(now)),
            )
        return PairingCode(
            id=identifier,
            code=code,
            channel=channel,
            account=account,
            role=role,
            expires_at=expires,
        )

    def claim(self, channel: str, account: str, sender_id: str, code: str) -> Identity:
        if len(code) != 6 or not code.isdigit():
            raise AccessDeniedError("invalid pairing code")
        now = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM gateway_pairings WHERE channel=? AND account=?
                   AND consumed_at IS NULL AND expires_at>=? ORDER BY created_at DESC""",
                (channel, account, _iso(now)),
            ).fetchall()
            matched = None
            for row in rows:
                if row["attempts"] >= 5:
                    continue
                digest = hashlib.sha256(f"{row['salt']}:{code}".encode()).hexdigest()
                if secrets.compare_digest(digest, row["code_hash"]):
                    matched = row
                    break
                db.execute(
                    "UPDATE gateway_pairings SET attempts=attempts+1 WHERE id=?", (row["id"],)
                )
            if matched is None:
                raise AccessDeniedError("invalid or expired pairing code")
            identifier = str(uuid.uuid4())
            stamp = _iso(now)
            db.execute(
                "UPDATE gateway_pairings SET consumed_at=? WHERE id=?", (stamp, matched["id"])
            )
            db.execute(
                """INSERT INTO gateway_identities VALUES(?,?,?,?,?,1,?,?)
              ON CONFLICT(channel,account,sender_id) DO UPDATE SET role=excluded.role,
              enabled=1,updated_at=excluded.updated_at""",
                (identifier, channel, account, sender_id, matched["role"], stamp, stamp),
            )
            db.commit()
        return self.get(channel, account, sender_id)

    def get(self, channel: str, account: str, sender_id: str) -> Identity:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM gateway_identities WHERE channel=? AND account=? AND sender_id=?",
                (channel, account, sender_id),
            ).fetchone()
        if row is None:
            raise AccessDeniedError("identity is not paired")
        return Identity(
            id=row["id"],
            channel=row["channel"],
            account=row["account"],
            sender_id=row["sender_id"],
            role=row["role"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def authorize(
        self, channel: str, account: str, sender_id: str, permission: str = "agent.run"
    ) -> Identity:
        identity = self.get(channel, account, sender_id)
        if not identity.enabled or permission not in _PERMISSIONS[identity.role]:
            raise AccessDeniedError(f"permission denied: {permission}")
        return identity

    def revoke(self, identity_id: str) -> bool:
        with self._db() as db:
            changed = db.execute(
                "UPDATE gateway_identities SET enabled=0,updated_at=? WHERE id=? AND enabled=1",
                (_iso(_now()), identity_id),
            ).rowcount
        return bool(changed)

    def list(self) -> list[Identity]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM gateway_identities ORDER BY created_at").fetchall()
        return [self.get(r["channel"], r["account"], r["sender_id"]) for r in rows]
