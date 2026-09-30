from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from unknown_agent.release import (
    Migration,
    MigrationError,
    MigrationManager,
    SupplyChainError,
    create_attestation,
    generate_spdx,
    inventory,
    verify_attestation,
)


def test_transactional_checksum_locked_migrations_and_backup(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    migrations = [
        Migration(
            version=1,
            name="create_items",
            statements=("CREATE TABLE items(id INTEGER PRIMARY KEY)",),
        ),
        Migration(
            version=2, name="add_name", statements=("ALTER TABLE items ADD COLUMN name TEXT",)
        ),
    ]
    manager = MigrationManager(database, migrations)
    dry = manager.apply(dry_run=True)
    assert dry.applied == (1, 2) and not database.with_suffix(".bak").exists()
    result = manager.apply()
    assert result.applied == (1, 2) and result.backup and result.backup.is_file()
    assert manager.apply().applied == ()
    changed = [migrations[0], Migration(version=2, name="changed", statements=("SELECT 1",))]
    with pytest.raises(MigrationError, match="checksum changed"):
        MigrationManager(database, changed).pending()


def test_failed_migration_rolls_back_and_backup_restores(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = MigrationManager(
        database,
        [Migration(version=1, name="base", statements=("CREATE TABLE stable(value TEXT)",))],
    )
    first.apply()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO stable VALUES('before')")
    backup = first.create_backup()
    manager = MigrationManager(
        database,
        [
            Migration(version=1, name="base", statements=("CREATE TABLE stable(value TEXT)",)),
            Migration(
                version=2,
                name="broken",
                statements=("CREATE TABLE transient(value TEXT)", "INVALID SQL"),
            ),
        ],
    )
    with pytest.raises(MigrationError, match="failed"):
        manager.apply(backup=False)
    with sqlite3.connect(database) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='transient'").fetchone() is None
        )
        db.execute("DELETE FROM stable")
    first.restore(backup)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM stable").fetchone()[0] == "before"


def project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "unknown_agent").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="1.0"\nlicense={text="MIT"}\ndependencies=["pydantic>=2"]\n'
    )
    (tmp_path / "src" / "unknown_agent" / "version.py").write_text('__version__ = "1.0"\n')
    (tmp_path / "artifact.txt").write_text("release bytes")
    return tmp_path


def test_spdx_is_deterministic_except_explicit_timestamp(tmp_path: Path) -> None:
    root = project(tmp_path)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    first = generate_spdx(root, created_at=stamp)
    second = generate_spdx(root, created_at=stamp)
    assert first == second
    assert first["spdxVersion"] == "SPDX-2.3"
    assert first["files"] and first["relationships"]


def test_dsse_attestation_signs_and_verifies_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path)
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()
    monkeypatch.setenv("ATTEST_KEY", raw)
    artifacts = inventory(root)
    envelope = create_attestation(
        artifacts,
        private_key_ref="env:ATTEST_KEY",
        builder_id="https://unknown-agent.dev/builder/test",
        source_uri="https://github.com/flormariap/unknown-agent",
        source_revision="abc123",
    )
    statement = verify_attestation(envelope, artifacts)
    assert statement["predicateType"] == "https://slsa.dev/provenance/v1"
    tampered = json.loads(json.dumps(envelope))
    tampered["payload"] = tampered["payload"][:-2] + "AA"
    with pytest.raises(SupplyChainError, match="invalid"):
        verify_attestation(tampered, artifacts)
    (root / "artifact.txt").write_text("changed")
    with pytest.raises(SupplyChainError, match="subjects"):
        verify_attestation(envelope, inventory(root))
