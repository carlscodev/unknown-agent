from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from unknown_agent.extensions.manifest import (
    CapabilityDeclaration,
    TrustStore,
    build_manifest,
    sign_manifest,
)
from unknown_agent.marketplace import (
    Marketplace,
    MarketplaceError,
    MarketplaceIndex,
    PackageRelease,
)


def keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(public).decode()


def signed_index(
    private: Ed25519PrivateKey, release: PackageRelease, *, revoked: tuple[str, ...] = ()
) -> MarketplaceIndex:
    now = datetime.now(UTC)
    unsigned = MarketplaceIndex(
        generated_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(hours=1),
        releases=(release,),
        revoked_publishers=revoked,
        signature="",
    )
    return unsigned.model_copy(
        update={"signature": base64.b64encode(private.sign(unsigned.signing_bytes())).decode()}
    )


def plugin_package(
    tmp_path: Path, publisher_private: Ed25519PrivateKey, publisher_public: str
) -> bytes:
    source = tmp_path / "demo.py"
    source.write_text("def register(api):\n    return None\n")
    raw_public = base64.b64decode(publisher_public)
    key_id = hashlib.sha256(raw_public).hexdigest()[:32]
    private_file = tmp_path / "publisher.key"
    private_file.write_text(
        base64.b64encode(
            publisher_private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
    )
    manifest = build_manifest(
        source,
        name="demo",
        version="1.0.0",
        entrypoint="demo:register",
        publisher="Example Publisher",
        public_key=publisher_public,
        permissions=["tools.register", "tools.read"],
        capabilities=[CapabilityDeclaration(kind="tool", name="demo.read", risk="read")],
    )
    assert manifest.key_id == key_id
    signed = sign_manifest(manifest, private_file)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("demo.py", source.read_bytes())
        archive.writestr("demo.manifest.json", signed.to_json())
    return output.getvalue()


def marketplace(tmp_path: Path, registry_public: str, publisher_public: str) -> Marketplace:
    trust = TrustStore(tmp_path / "trust.json")
    trust.trust(publisher_public, publisher="Example Publisher")
    return Marketplace(
        tmp_path / "plugins",
        tmp_path / "marketplace.json",
        trust,
        registry_public,
    )


def test_signed_index_verified_and_tamper_rejected(tmp_path: Path) -> None:
    registry_private, registry_public = keypair()
    publisher_private, publisher_public = keypair()
    package = plugin_package(tmp_path, publisher_private, publisher_public)
    release = PackageRelease(
        name="demo",
        version="1.0.0",
        url="https://market.example/demo.zip",
        sha256=hashlib.sha256(package).hexdigest(),
        publisher_key_id=hashlib.sha256(base64.b64decode(publisher_public)).hexdigest()[:32],
    )
    market = marketplace(tmp_path, registry_public, publisher_public)
    index = signed_index(registry_private, release)
    verified = market.verify_index(index.model_dump_json().encode())
    assert market.select(verified, "demo").version == "1.0.0"
    payload = json.loads(index.model_dump_json())
    payload["releases"][0]["version"] = "9.9.9"
    with pytest.raises(MarketplaceError, match="signature"):
        market.verify_index(json.dumps(payload).encode())


def test_atomic_verified_install_and_uninstall(tmp_path: Path) -> None:
    registry_private, registry_public = keypair()
    publisher_private, publisher_public = keypair()
    package = plugin_package(tmp_path, publisher_private, publisher_public)
    key_id = hashlib.sha256(base64.b64decode(publisher_public)).hexdigest()[:32]
    release = PackageRelease(
        name="demo",
        version="1.0.0",
        url="https://market.example/demo.zip",
        sha256=hashlib.sha256(package).hexdigest(),
        publisher_key_id=key_id,
    )
    market = marketplace(tmp_path, registry_public, publisher_public)
    index = signed_index(registry_private, release)
    installed = market.install_bytes(index, release, package)
    assert installed.source_path.is_file() and installed.manifest_path.is_file()
    assert market.installed()["demo"].version == "1.0.0"
    assert market.uninstall("demo")
    assert not installed.source_path.exists()


def test_revocation_hash_and_archive_traversal_fail_closed(tmp_path: Path) -> None:
    registry_private, registry_public = keypair()
    publisher_private, publisher_public = keypair()
    package = plugin_package(tmp_path, publisher_private, publisher_public)
    key_id = hashlib.sha256(base64.b64decode(publisher_public)).hexdigest()[:32]
    release = PackageRelease(
        name="demo",
        version="1.0.0",
        url="https://market.example/demo.zip",
        sha256=hashlib.sha256(package).hexdigest(),
        publisher_key_id=key_id,
    )
    market = marketplace(tmp_path, registry_public, publisher_public)
    revoked = signed_index(registry_private, release, revoked=(key_id,))
    with pytest.raises(MarketplaceError, match="revoked"):
        market.install_bytes(revoked, release, package)
    index = signed_index(registry_private, release)
    with pytest.raises(MarketplaceError, match="SHA-256"):
        market.install_bytes(index, release, package + b"tamper")
    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.py", "bad")
    unsafe_bytes = unsafe.getvalue()
    unsafe_release = release.model_copy(update={"sha256": hashlib.sha256(unsafe_bytes).hexdigest()})
    with pytest.raises(MarketplaceError, match="unsafe"):
        market.install_bytes(index, unsafe_release, unsafe_bytes)
    assert not (tmp_path / "escape.py").exists()
