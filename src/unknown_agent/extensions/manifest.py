"""Signed capability manifests and publisher trust for extensions.

A plugin file is untrusted bytes until all checks pass: deterministic Pydantic
validation, runtime API compatibility, SHA-256 content binding, a trusted
publisher key and an Ed25519 signature.  Verification never imports plugin code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

RUNTIME_CAPABILITY_API = "1"
Permission = Literal[
    "tools.register",
    "tools.read",
    "tools.write",
    "tools.execute",
    "tools.network",
    "tools.destructive",
    "providers.register",
    "gates.register",
    "commands.register",
    "memory.extend",
]
CapabilityKind = Literal["tool", "provider", "gate", "command", "fact_kind"]
ToolRisk = Literal["read", "write", "execute", "network", "destructive"]


class CapabilityDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CapabilityKind
    name: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")]
    risk: ToolRisk | None = None


class CapabilityManifest(BaseModel):
    """Canonical signed contract distributed beside plugin code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.-]*$")]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")]
    runtime_api: Annotated[str, Field(min_length=1, max_length=32)] = RUNTIME_CAPABILITY_API
    entrypoint: Annotated[str, Field(min_length=1, max_length=256)]
    publisher: Annotated[str, Field(min_length=1, max_length=128)]
    key_id: Annotated[str, Field(min_length=16, max_length=64, pattern=r"^[a-f0-9]+$")]
    content_sha256: Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")]
    permissions: list[Permission] = Field(default_factory=list)
    capabilities: list[CapabilityDeclaration] = Field(default_factory=list)
    issued_at: int = Field(gt=0)
    signature: str = ""

    @field_validator("permissions")
    @classmethod
    def unique_permissions(cls, value: list[Permission]) -> list[Permission]:
        if len(value) != len(set(value)):
            raise ValueError("permissions must be unique")
        return sorted(value)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(
        cls, value: list[CapabilityDeclaration]
    ) -> list[CapabilityDeclaration]:
        keys = [(item.kind, item.name) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("capability kind/name pairs must be unique")
        for item in value:
            if item.kind == "tool" and item.risk is None:
                raise ValueError(f"tool capability {item.name!r} must declare risk")
            if item.kind != "tool" and item.risk is not None:
                raise ValueError(f"non-tool capability {item.name!r} cannot declare risk")
        return sorted(value, key=lambda item: (item.kind, item.name))

    def signing_bytes(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        return _canonical(payload).encode()

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    def declares(self, kind: CapabilityKind, name: str) -> CapabilityDeclaration | None:
        return next(
            (item for item in self.capabilities if item.kind == kind and item.name == name),
            None,
        )


class TrustedPublisher(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str
    publisher: str
    public_key: str
    added_at: int


class ManifestVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    manifest: CapabilityManifest | None = None
    errors: list[str] = Field(default_factory=list)
    manifest_path: str = ""
    source_path: str = ""


class TrustStore:
    """Atomic local store of explicitly trusted Ed25519 publisher keys."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def entries(self) -> dict[str, TrustedPublisher]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("invalid publisher trust store schema")
        publishers = payload.get("publishers")
        if not isinstance(publishers, dict):
            raise ValueError("invalid publisher trust store entries")
        return {
            key: TrustedPublisher.model_validate(value) for key, value in publishers.items()
        }

    def get(self, key_id: str) -> TrustedPublisher | None:
        return self.entries().get(key_id)

    def trust(self, public_key: str, *, publisher: str) -> TrustedPublisher:
        raw = _decode_key(public_key, expected=32)
        key_id = hashlib.sha256(raw).hexdigest()[:32]
        entry = TrustedPublisher(
            key_id=key_id,
            publisher=publisher,
            public_key=base64.b64encode(raw).decode(),
            added_at=int(time.time()),
        )
        entries = self.entries()
        existing = entries.get(key_id)
        if existing is not None and existing.publisher != publisher:
            raise ValueError(
                f"key {key_id} is already trusted for publisher {existing.publisher!r}"
            )
        entries[key_id] = entry
        self._write(entries)
        return entry

    def revoke(self, key_id: str) -> bool:
        entries = self.entries()
        removed = entries.pop(key_id, None) is not None
        if removed:
            self._write(entries)
        return removed

    def _write(self, entries: dict[str, TrustedPublisher]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "publishers": {
                key: value.model_dump(mode="json") for key, value in sorted(entries.items())
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


def generate_keypair(private_path: str | Path, public_path: str | Path) -> tuple[str, str]:
    """Generate raw Ed25519 keys, refusing to overwrite either destination."""

    private_target = Path(private_path)
    public_target = Path(public_path)
    if private_target.exists() or public_target.exists():
        raise FileExistsError("refusing to overwrite an existing signing key")
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private_target.parent.mkdir(parents=True, exist_ok=True)
    public_target.parent.mkdir(parents=True, exist_ok=True)
    _exclusive_write(private_target, base64.b64encode(private_raw).decode() + "\n", mode=0o600)
    try:
        _exclusive_write(
            public_target, base64.b64encode(public_raw).decode() + "\n", mode=0o644
        )
    except Exception:
        private_target.unlink(missing_ok=True)
        raise
    return hashlib.sha256(public_raw).hexdigest()[:32], base64.b64encode(public_raw).decode()


def sign_manifest(manifest: CapabilityManifest, private_key: str | Path) -> CapabilityManifest:
    raw = _decode_key(Path(private_key).read_text(encoding="utf-8").strip(), expected=32)
    private = Ed25519PrivateKey.from_private_bytes(raw)
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    expected_key_id = hashlib.sha256(public_raw).hexdigest()[:32]
    if manifest.key_id != expected_key_id:
        raise ValueError(
            f"manifest key_id {manifest.key_id} does not match private key {expected_key_id}"
        )
    signature = base64.b64encode(private.sign(manifest.signing_bytes())).decode()
    return manifest.model_copy(update={"signature": signature})


def build_manifest(
    source_path: str | Path,
    *,
    name: str,
    version: str,
    entrypoint: str,
    publisher: str,
    public_key: str,
    permissions: list[Permission],
    capabilities: list[CapabilityDeclaration],
    runtime_api: str = RUNTIME_CAPABILITY_API,
) -> CapabilityManifest:
    source = Path(source_path)
    raw_key = _decode_key(public_key, expected=32)
    return CapabilityManifest(
        name=name,
        version=version,
        runtime_api=runtime_api,
        entrypoint=entrypoint,
        publisher=publisher,
        key_id=hashlib.sha256(raw_key).hexdigest()[:32],
        content_sha256=_file_hash(source),
        permissions=permissions,
        capabilities=capabilities,
        issued_at=int(time.time()),
    )


def verify_manifest(
    manifest_path: str | Path,
    source_path: str | Path,
    trust_store: TrustStore,
    *,
    expected_entrypoint: str = "",
    runtime_api: str = RUNTIME_CAPABILITY_API,
) -> ManifestVerification:
    manifest_file = Path(manifest_path)
    source = Path(source_path)
    errors: list[str] = []
    try:
        manifest = CapabilityManifest.model_validate_json(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed untrusted boundary
        return ManifestVerification(
            ok=False,
            errors=[f"invalid manifest: {type(exc).__name__}: {exc}"],
            manifest_path=str(manifest_file),
            source_path=str(source),
        )
    if not _runtime_compatible(manifest.runtime_api, runtime_api):
        errors.append(
            f"runtime API {manifest.runtime_api!r} is incompatible with {runtime_api!r}"
        )
    if expected_entrypoint and manifest.entrypoint != expected_entrypoint:
        errors.append(
            f"entrypoint mismatch: manifest has {manifest.entrypoint!r}, discovered {expected_entrypoint!r}"
        )
    actual_hash = _file_hash(source)
    if actual_hash != manifest.content_sha256:
        errors.append("plugin content hash does not match signed manifest")
    trusted = trust_store.get(manifest.key_id)
    if trusted is None:
        errors.append(f"publisher key {manifest.key_id} is not trusted")
    elif trusted.publisher != manifest.publisher:
        errors.append(
            f"trusted publisher is {trusted.publisher!r}, manifest claims {manifest.publisher!r}"
        )
    if not manifest.signature:
        errors.append("manifest has no signature")
    elif trusted is not None:
        try:
            public_raw = _decode_key(trusted.public_key, expected=32)
            signature = _decode_key(manifest.signature, expected=64)
            Ed25519PublicKey.from_public_bytes(public_raw).verify(
                signature, manifest.signing_bytes()
            )
        except Exception:  # noqa: BLE001 - do not leak crypto backend details
            errors.append("manifest signature verification failed")
    return ManifestVerification(
        ok=not errors,
        manifest=manifest,
        errors=errors,
        manifest_path=str(manifest_file),
        source_path=str(source),
    )


def required_tool_permission(risk: str) -> Permission:
    mapping: dict[str, Permission] = {
        "read": "tools.read",
        "write": "tools.write",
        "execute": "tools.execute",
        "network": "tools.network",
        "destructive": "tools.destructive",
    }
    if risk not in mapping:
        raise ValueError(f"unknown tool risk {risk!r}")
    return mapping[risk]


def default_manifest_path(source: str | Path) -> Path:
    path = Path(source)
    return path.with_suffix(".manifest.json")


def _runtime_compatible(requirement: str, current: str) -> bool:
    if requirement == current:
        return True
    try:
        current_major = int(current.split(".", 1)[0])
    except ValueError:
        return False
    match = re.fullmatch(r">=(\d+),<(\d+)", requirement.replace(" ", ""))
    return bool(match and int(match.group(1)) <= current_major < int(match.group(2)))


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_key(value: str, *, expected: int) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid base64 key material") from exc
    if len(raw) != expected:
        raise ValueError(f"key material must decode to {expected} bytes")
    return raw


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _exclusive_write(path: Path, content: str, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


__all__ = [
    "CapabilityDeclaration",
    "CapabilityKind",
    "CapabilityManifest",
    "Permission",
    "ManifestVerification",
    "Permission",
    "RUNTIME_CAPABILITY_API",
    "TrustStore",
    "ToolRisk",
    "TrustedPublisher",
    "build_manifest",
    "default_manifest_path",
    "generate_keypair",
    "required_tool_permission",
    "sign_manifest",
    "verify_manifest",
]
