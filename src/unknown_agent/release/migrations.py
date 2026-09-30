"""Checksum-locked transactional SQLite schema migrations."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MigrationError(RuntimeError):
    """Migration history or execution is unsafe."""


class Migration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(gt=0)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    statements: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def non_empty_statements(self) -> Migration:
        if any(not statement.strip() for statement in self.statements):
            raise ValueError("migration statements cannot be empty")
        return self

    @property
    def checksum(self) -> str:
        canonical = "\n-- statement --\n".join(item.strip() for item in self.statements)
        return hashlib.sha256(f"{self.version}:{self.name}:{canonical}".encode()).hexdigest()


class MigrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    applied: tuple[int, ...]
    current_version: int
    backup: Path | None = None


class MigrationManager:
    def __init__(self, database: Path, migrations: list[Migration]) -> None:
        self.database = database
        self.migrations = sorted(migrations, key=lambda item: item.version)
        versions = [item.version for item in self.migrations]
        if len(versions) != len(set(versions)):
            raise ValueError("migration versions must be unique")
        if versions and versions != list(range(1, versions[-1] + 1)):
            raise ValueError("migration versions must be contiguous from 1")

    def pending(self) -> list[Migration]:
        self._initialize()
        applied = self._applied()
        known = {migration.version: migration for migration in self.migrations}
        for version, checksum in applied.items():
            migration = known.get(version)
            if migration is None:
                raise MigrationError(f"database contains unknown migration {version}")
            if not secrets_equal(checksum, migration.checksum):
                raise MigrationError(f"migration {version} checksum changed")
        current = max(applied, default=0)
        if sorted(applied) != list(range(1, current + 1)):
            raise MigrationError("migration history is not linear")
        return [item for item in self.migrations if item.version not in applied]

    def apply(self, *, backup: bool = True, dry_run: bool = False) -> MigrationResult:
        pending = self.pending()
        current = max(self._applied(), default=0)
        if dry_run or not pending:
            return MigrationResult(
                applied=tuple(item.version for item in pending) if dry_run else (),
                current_version=pending[-1].version if dry_run and pending else current,
            )
        backup_path = self.create_backup() if backup and self.database.exists() else None
        completed: list[int] = []
        try:
            with self._connect() as db:
                for migration in pending:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        for statement in migration.statements:
                            db.execute(statement)
                        db.execute(
                            "INSERT INTO schema_migrations VALUES(?,?,?,?)",
                            (
                                migration.version,
                                migration.name,
                                migration.checksum,
                                datetime.now(UTC).isoformat(),
                            ),
                        )
                        db.commit()
                        completed.append(migration.version)
                    except Exception:
                        db.rollback()
                        raise
        except Exception as exc:
            raise MigrationError(f"migration failed after versions {completed}: {exc}") from exc
        return MigrationResult(
            applied=tuple(completed),
            current_version=completed[-1] if completed else current,
            backup=backup_path,
        )

    def create_backup(self) -> Path:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.database.with_name(f"{self.database.name}.{stamp}.bak")
        source_db = self._connect()
        backup_db = sqlite3.connect(destination)
        try:
            source_db.backup(backup_db)
        finally:
            backup_db.close()
            source_db.close()
        return destination

    def restore(self, backup: Path) -> None:
        if not backup.is_file() or backup.suffix != ".bak":
            raise MigrationError("a valid .bak file is required")
        probe = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            result = probe.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise MigrationError("backup failed SQLite integrity check")
        finally:
            probe.close()
        temporary = self.database.with_suffix(self.database.suffix + ".restore")
        shutil.copyfile(backup, temporary)
        for suffix in ("-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)
        temporary.replace(self.database)

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations(
                version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL)"""
            )

    def _applied(self) -> dict[int, str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT version,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        return {int(row[0]): str(row[1]) for row in rows}

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=30, isolation_level=None)


def secrets_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
