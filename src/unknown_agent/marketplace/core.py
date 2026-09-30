"""Signed marketplace index and atomic verified plugin installation."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unknown_agent.extensions.manifest import TrustStore, verify_manifest

_MAX_INDEX_BYTES = 5_000_000
_MAX_PACKAGE_BYTES = 50_000_000
_MAX_FILES = 1000


class MarketplaceError(RuntimeError):
    """Marketplace authenticity, policy, or installation failure."""


class PackageRelease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    url: str = Field(pattern=r"^https://")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    publisher_key_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    yanked: bool = False


class MarketplaceIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    generated_at: datetime
    expires_at: datetime
    releases: tuple[PackageRelease, ...] = Field(max_length=100_000)
    revoked_publishers: tuple[str, ...] = Field(default=(), max_length=10_000)
    signature: str

    @field_validator("releases")
    @classmethod
    def unique_releases(cls, value: tuple[PackageRelease, ...]) -> tuple[PackageRelease, ...]:
        identities = [(item.name, item.version) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("marketplace releases must be unique")
        return tuple(sorted(value, key=lambda item: (item.name, _version(item.version))))

    def signing_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json", exclude={"signature"}))


class InstalledPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    version: str
    source_path: Path
    manifest_path: Path
    installed_at: datetime
    package_sha256: str


class Marketplace:
    def __init__(
        self,
        install_dir: Path,
        state_file: Path,
        trust_store: TrustStore,
        registry_public_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        if not 0 < timeout_seconds <= 300:
            raise ValueError("marketplace timeout must be between 0 and 300 seconds")
        self.install_dir = install_dir
        self.state_file = state_file
        self.trust_store = trust_store
        self.registry_key = _decode_public_key(registry_public_key)
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    async def fetch_index(self, url: str) -> MarketplaceIndex:
        if not url.startswith("https://"):
            raise MarketplaceError("marketplace index requires HTTPS")
        content = await self._download(url, _MAX_INDEX_BYTES, "marketplace index")
        return self.verify_index(content)

    def verify_index(self, content: bytes) -> MarketplaceIndex:
        try:
            index = MarketplaceIndex.model_validate_json(content)
            signature = base64.b64decode(index.signature, validate=True)
            self.registry_key.verify(signature, index.signing_bytes())
        except (ValueError, InvalidSignature) as exc:
            raise MarketplaceError("marketplace index signature verification failed") from exc
        now = datetime.now(UTC)
        if index.generated_at > now or index.expires_at <= now:
            raise MarketplaceError("marketplace index is not currently valid")
        if index.expires_at.timestamp() - index.generated_at.timestamp() > 86_400:
            raise MarketplaceError("marketplace index validity exceeds 24 hours")
        return index

    async def install(
        self,
        index: MarketplaceIndex,
        name: str,
        *,
        version: str | None = None,
        allow_downgrade: bool = False,
    ) -> InstalledPackage:
        release = self.select(index, name, version=version)
        package = await self._download(release.url, _MAX_PACKAGE_BYTES, "marketplace package")
        return self.install_bytes(index, release, package, allow_downgrade=allow_downgrade)

    async def _download(self, url: str, limit: int, label: str) -> bytes:
        if not url.startswith("https://"):
            raise MarketplaceError(f"{label} requires HTTPS")
        retained = bytearray()
        async with self.client.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > limit:
                        raise MarketplaceError(f"{label} exceeds size limit")
                except ValueError as exc:
                    raise MarketplaceError(f"{label} has invalid content length") from exc
            async for chunk in response.aiter_bytes():
                retained.extend(chunk)
                if len(retained) > limit:
                    raise MarketplaceError(f"{label} exceeds size limit")
        return bytes(retained)

    def select(
        self, index: MarketplaceIndex, name: str, *, version: str | None = None
    ) -> PackageRelease:
        candidates = [
            item
            for item in index.releases
            if item.name == name
            and not item.yanked
            and (version is None or item.version == version)
        ]
        if not candidates:
            raise MarketplaceError("requested marketplace release was not found")
        release = max(candidates, key=lambda item: _version(item.version))
        if release.publisher_key_id in index.revoked_publishers:
            raise MarketplaceError("release publisher is revoked")
        return release

    def install_bytes(
        self,
        index: MarketplaceIndex,
        release: PackageRelease,
        package: bytes,
        *,
        allow_downgrade: bool = False,
    ) -> InstalledPackage:
        if release.publisher_key_id in index.revoked_publishers:
            raise MarketplaceError("release publisher is revoked")
        digest = hashlib.sha256(package).hexdigest()
        if not secrets_equal(digest, release.sha256):
            raise MarketplaceError("marketplace package SHA-256 mismatch")
        installed = self.installed().get(release.name)
        if (
            installed
            and _version(release.version) < _version(installed.version)
            and not allow_downgrade
        ):
            raise MarketplaceError("marketplace downgrade requires explicit approval")
        self.install_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".marketplace-", dir=self.install_dir))
        try:
            source, manifest = self._extract(package, temporary)
            verification = verify_manifest(manifest, source, self.trust_store)
            if not verification.ok or verification.manifest is None:
                raise MarketplaceError(
                    "plugin verification failed: " + "; ".join(verification.errors)
                )
            signed = verification.manifest
            if signed.name != release.name or signed.version != release.version:
                raise MarketplaceError("package identity does not match marketplace index")
            if signed.key_id != release.publisher_key_id:
                raise MarketplaceError("package publisher does not match marketplace index")
            target_source = self.install_dir / f"{release.name.replace('-', '_')}.py"
            target_manifest = target_source.with_suffix(".manifest.json")
            backup_source = temporary / ".previous.py"
            backup_manifest = temporary / ".previous.manifest.json"
            if target_source.exists():
                target_source.replace(backup_source)
            if target_manifest.exists():
                target_manifest.replace(backup_manifest)
            try:
                source.replace(target_source)
                manifest.replace(target_manifest)
                record = InstalledPackage(
                    name=release.name,
                    version=release.version,
                    source_path=target_source,
                    manifest_path=target_manifest,
                    installed_at=datetime.now(UTC),
                    package_sha256=digest,
                )
                values = self.installed()
                values[record.name] = record
                self._write_state(values)
                return record
            except Exception:
                target_source.unlink(missing_ok=True)
                target_manifest.unlink(missing_ok=True)
                if backup_source.exists():
                    backup_source.replace(target_source)
                if backup_manifest.exists():
                    backup_manifest.replace(target_manifest)
                raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def uninstall(self, name: str) -> bool:
        values = self.installed()
        record = values.pop(name, None)
        if record is None:
            return False
        record.source_path.unlink(missing_ok=True)
        record.manifest_path.unlink(missing_ok=True)
        self._write_state(values)
        return True

    def installed(self) -> dict[str, InstalledPackage]:
        if not self.state_file.exists():
            return {}
        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not isinstance(payload.get("packages"), dict):
            raise MarketplaceError("invalid marketplace state")
        return {
            name: InstalledPackage.model_validate(value)
            for name, value in payload["packages"].items()
        }

    def _extract(self, package: bytes, destination: Path) -> tuple[Path, Path]:
        archive_path = destination / "package.zip"
        archive_path.write_bytes(package)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > _MAX_FILES
                    or sum(item.file_size for item in entries) > _MAX_PACKAGE_BYTES
                ):
                    raise MarketplaceError("marketplace archive exceeds extraction limits")
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    if path.is_absolute() or ".." in path.parts or entry.is_dir():
                        if entry.is_dir() and not path.is_absolute() and ".." not in path.parts:
                            continue
                        raise MarketplaceError("unsafe marketplace archive path")
                    if len(path.parts) != 1:
                        raise MarketplaceError("marketplace archive must be flat")
                    target = destination / path.name
                    with archive.open(entry) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        except zipfile.BadZipFile as exc:
            raise MarketplaceError("invalid marketplace ZIP package") from exc
        sources = list(destination.glob("*.py"))
        manifests = list(destination.glob("*.manifest.json"))
        if len(sources) != 1 or len(manifests) != 1:
            raise MarketplaceError("package must contain one plugin and one manifest")
        return sources[0], manifests[0]

    def _write_state(self, values: dict[str, InstalledPackage]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "packages": {
                name: value.model_dump(mode="json") for name, value in sorted(values.items())
            },
        }
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.state_file)


def _decode_public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise ValueError("registry public key must be base64 Ed25519") from exc


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _version(value: str) -> tuple[int, int, int, str]:
    core, _, suffix = value.replace("+", "-").partition("-")
    major, minor, patch = core.split(".")
    return int(major), int(minor), int(patch), suffix


def secrets_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
