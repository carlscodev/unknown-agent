# Release supply chain

Unknown Agent ships deterministic inventory, schema migration, SBOM, and provenance primitives for auditable releases.

## Database migrations

`MigrationManager` applies contiguous, immutable migration definitions. Every applied migration stores a SHA-256 checksum; editing historical SQL causes startup to fail closed. Each migration runs under `BEGIN IMMEDIATE`, records its version in the same transaction, and rolls back on any failed statement. Production applies create a SQLite-native backup first. Restore validates `PRAGMA integrity_check`, removes stale WAL sidecars, and atomically replaces the database.

Dry runs return the exact pending versions without changing application schema. Migration and SQLite lock timeouts are bounded to 30 seconds.

## SPDX SBOM

```bash
ua release sbom --output sbom.spdx.json
```

The generator emits SPDX 2.3 JSON with project metadata, declared Python dependencies, source-file SHA-256 checksums, package relationships, and a content-derived document namespace. Build outputs, VCS state, local runtime state, virtual environments, and dependency caches are excluded.

## Signed provenance

Store a raw 32-byte Ed25519 private key as a supported secret reference, then run:

```bash
ua release attest \
  --key-ref env:UNKNOWN_AGENT_ATTEST_KEY \
  --revision "$(git rev-parse HEAD)" \
  --output provenance.intoto.jsonl
```

The output is a DSSE envelope containing an in-toto Statement v1 and SLSA Provenance v1 predicate. Subjects bind every release input to SHA-256, while resolved dependencies bind the canonical repository and Git revision. The public key and its SHA-256 key identifier accompany the signature; the private key is never persisted.

Verify both the Ed25519 signature and the exact current artifact inventory:

```bash
ua release verify-attestation provenance.intoto.jsonl
```

Verification fails if the envelope, signature, key identifier, subject path, or artifact digest differs.