"""Deterministic SPDX SBOM and signed in-toto release attestations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from unknown_agent.config.secrets import resolve_secret

_EXCLUDED = {".git", ".unknown", ".venv", "node_modules", "dist", "build", "__pycache__"}


class SupplyChainError(RuntimeError):
    """SBOM or attestation validation failed."""


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)


def inventory(root: Path) -> list[Artifact]:
    resolved = root.resolve()
    artifacts: list[Artifact] = []
    for path in sorted(item for item in resolved.rglob("*") if item.is_file()):
        relative = path.relative_to(resolved)
        if any(part in _EXCLUDED for part in relative.parts):
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
        artifacts.append(Artifact(path=relative.as_posix(), sha256=digest.hexdigest(), size=size))
    return artifacts


def generate_spdx(root: Path, *, created_at: datetime | None = None) -> dict[str, Any]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise SupplyChainError("pyproject.toml not found")
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    name = str(project["name"])
    version = _project_version(root)
    files = inventory(root)
    dependencies = sorted(str(item) for item in project.get("dependencies", []))
    namespace_digest = hashlib.sha256(
        "\n".join(f"{item.path}:{item.sha256}" for item in files).encode()
    ).hexdigest()
    document_id = "SPDXRef-DOCUMENT"
    package_id = "SPDXRef-Package-Unknown-Agent"
    packages: list[dict[str, Any]] = [
        {
            "SPDXID": package_id,
            "name": name,
            "versionInfo": version,
            "downloadLocation": "https://github.com/flormariap/unknown-agent",
            "filesAnalyzed": True,
            "licenseDeclared": str(project.get("license", {}).get("text", "NOASSERTION")),
            "licenseConcluded": "NOASSERTION",
        }
    ]
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": document_id,
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": package_id,
        }
    ]
    for index, dependency in enumerate(dependencies, start=1):
        dependency_id = f"SPDXRef-Dependency-{index}"
        packages.append(
            {
                "SPDXID": dependency_id,
                "name": dependency,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseDeclared": "NOASSERTION",
                "licenseConcluded": "NOASSERTION",
            }
        )
        relationships.append(
            {
                "spdxElementId": package_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": dependency_id,
            }
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": document_id,
        "name": f"{name}-{version}",
        "documentNamespace": f"https://unknown-agent.dev/spdx/{namespace_digest}",
        "creationInfo": {
            "created": (created_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
            "creators": ["Tool: unknown-agent"],
        },
        "packages": packages,
        "files": [
            {
                "SPDXID": f"SPDXRef-File-{index}",
                "fileName": artifact.path,
                "checksums": [{"algorithm": "SHA256", "checksumValue": artifact.sha256}],
            }
            for index, artifact in enumerate(files, start=1)
        ],
        "relationships": relationships,
    }


def create_attestation(
    artifacts: list[Artifact],
    *,
    private_key_ref: str,
    builder_id: str,
    source_uri: str,
    source_revision: str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    private_key = _private_key(private_key_ref)
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_id = hashlib.sha256(public).hexdigest()
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": item.path, "digest": {"sha256": item.sha256}} for item in artifacts],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://unknown-agent.dev/build/v1",
                "externalParameters": {},
                "internalParameters": {},
                "resolvedDependencies": [
                    {"uri": source_uri, "digest": {"gitCommit": source_revision}}
                ],
            },
            "runDetails": {
                "builder": {"id": builder_id},
                "metadata": {
                    "invocationId": os.environ.get("GITHUB_RUN_ID", "local"),
                    "startedOn": (created_at or datetime.now(UTC))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            },
        },
    }
    payload = canonical_json(statement)
    payload_type = "application/vnd.in-toto+json"
    signature = private_key.sign(_pae(payload_type, payload))
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload).decode(),
        "signatures": [
            {
                "keyid": key_id,
                "sig": base64.b64encode(signature).decode(),
                "publicKey": public.hex(),
            }
        ],
    }


def verify_attestation(envelope: dict[str, Any], artifacts: list[Artifact]) -> dict[str, Any]:
    try:
        payload_type = str(envelope["payloadType"])
        payload = base64.b64decode(envelope["payload"], validate=True)
        signature = envelope["signatures"][0]
        public_raw = bytes.fromhex(signature["publicKey"])
        expected_key = hashlib.sha256(public_raw).hexdigest()
        if signature["keyid"] != expected_key:
            raise SupplyChainError("attestation key id mismatch")
        Ed25519PublicKey.from_public_bytes(public_raw).verify(
            base64.b64decode(signature["sig"], validate=True), _pae(payload_type, payload)
        )
        statement = json.loads(payload)
    except (KeyError, IndexError, ValueError, InvalidSignature, json.JSONDecodeError) as exc:
        raise SupplyChainError("invalid attestation signature or envelope") from exc
    expected = {item.path: item.sha256 for item in artifacts}
    actual = {
        str(item["name"]): str(item["digest"]["sha256"]) for item in statement.get("subject", [])
    }
    if actual != expected:
        raise SupplyChainError("attestation subjects do not match artifacts")
    return statement


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _pae(payload_type: str, payload: bytes) -> bytes:
    encoded_type = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(encoded_type), encoded_type, len(payload), payload)


def _private_key(reference: str) -> Ed25519PrivateKey:
    value = resolve_secret(reference)
    if not value:
        raise SupplyChainError("attestation signing key did not resolve")
    try:
        raw = bytes.fromhex(value)
        if len(raw) != 32:
            raise ValueError
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise SupplyChainError("signing key must be 32-byte Ed25519 hex") from exc


def _project_version(root: Path) -> str:
    version_file = root / "src" / "unknown_agent" / "version.py"
    if version_file.is_file():
        for line in version_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__") and "=" in line:
                return line.split("=", 1)[1].strip().strip("'\"")
    return "0+unknown"
