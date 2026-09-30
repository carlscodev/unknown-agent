"""Bounded content-addressed media storage and cached voice transcription."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unknown_agent.config.secrets import is_reference, resolve_secret
from unknown_agent.gateway.contracts import ContentKind, ContentPart

MediaResolver = Callable[[str], Awaitable["ResolvedMedia"]]


class MediaError(RuntimeError):
    """A media integrity, policy, or resolution failure."""


class MediaPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_bytes: int = Field(default=25_000_000, gt=0, le=100_000_000)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    allowed_mime_prefixes: tuple[str, ...] = (
        "image/",
        "audio/",
        "video/",
        "text/",
        "application/pdf",
    )


class MediaAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0, le=100_000_000)
    mime_type: str
    name: str | None = None
    path: Path
    created_at: datetime


class ResolvedMedia(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
    chunks: AsyncIterable[bytes]
    mime_type: str
    name: str | None = None
    size_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class MediaStore:
    def __init__(self, root: Path, database: Path, policy: MediaPolicy | None = None) -> None:
        self.root = root
        self.database = database
        self.policy = policy or MediaPolicy()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._resolvers: dict[str, MediaResolver] = {}
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS gateway_media(
                sha256 TEXT PRIMARY KEY,size_bytes INTEGER NOT NULL,mime_type TEXT NOT NULL,
                name TEXT,created_at TEXT NOT NULL)"""
            )

    def register_resolver(self, scheme: str, resolver: MediaResolver) -> None:
        normalized = scheme.rstrip(":").lower()
        if not normalized or normalized in {"cas", "file"}:
            raise ValueError("invalid or reserved media resolver scheme")
        self._resolvers[normalized] = resolver

    async def hydrate(self, part: ContentPart) -> tuple[ContentPart, MediaAsset]:
        if part.kind is ContentKind.TEXT or not part.uri:
            raise MediaError("only URI-backed media can be hydrated")
        scheme = part.uri.split(":", 1)[0].lower()
        if scheme == "cas":
            asset = self.get(part.uri.split(":", 1)[1])
        else:
            resolver = self._resolvers.get(scheme)
            if resolver is None:
                raise MediaError(f"no media resolver registered for {scheme}")
            resolved = await resolver(part.uri)
            asset = await self.ingest(
                resolved.chunks,
                mime_type=resolved.mime_type,
                name=resolved.name or part.name,
                declared_size=resolved.size_bytes or part.size_bytes,
                expected_sha256=resolved.sha256 or part.sha256,
            )
        hydrated = part.model_copy(
            update={
                "uri": f"cas:{asset.sha256}",
                "mime_type": asset.mime_type,
                "name": asset.name,
                "size_bytes": asset.size_bytes,
                "sha256": asset.sha256,
            }
        )
        return hydrated, asset

    async def ingest(
        self,
        chunks: AsyncIterable[bytes],
        *,
        mime_type: str,
        name: str | None = None,
        declared_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> MediaAsset:
        self._validate_mime(mime_type)
        if declared_size is not None and declared_size > self.policy.max_bytes:
            raise MediaError("declared media size exceeds policy")
        digest = hashlib.sha256()
        size = 0
        descriptor, temporary = tempfile.mkstemp(prefix=".media-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise MediaError("media resolver yielded a non-bytes chunk")
                    size += len(chunk)
                    if size > self.policy.max_bytes:
                        raise MediaError("media exceeds size policy")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if declared_size is not None and declared_size != size:
                raise MediaError("media size does not match declaration")
            sha256 = digest.hexdigest()
            if expected_sha256 and not hmac_compare(sha256, expected_sha256):
                raise MediaError("media SHA-256 mismatch")
            destination = self._path(sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                Path(temporary).unlink(missing_ok=True)
                self._verify_file(destination, sha256, size)
            else:
                Path(temporary).replace(destination)
            created = datetime.now(UTC)
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO gateway_media VALUES(?,?,?,?,?)",
                    (sha256, size, mime_type, name, created.isoformat()),
                )
            return self.get(sha256)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def get(self, sha256: str) -> MediaAsset:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise MediaError("invalid media digest")
        with self._connect() as db:
            row = db.execute("SELECT * FROM gateway_media WHERE sha256=?", (sha256,)).fetchone()
        if row is None:
            raise MediaError("media asset not found")
        path = self._path(sha256)
        self._verify_file(path, sha256, int(row["size_bytes"]))
        return MediaAsset(
            sha256=sha256,
            size_bytes=row["size_bytes"],
            mime_type=row["mime_type"],
            name=row["name"],
            path=path,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _verify_file(self, path: Path, expected: str, size: int) -> None:
        if not path.is_file() or path.stat().st_size != size:
            raise MediaError("stored media size integrity failure")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if not hmac_compare(digest.hexdigest(), expected):
            raise MediaError("stored media digest integrity failure")

    def _validate_mime(self, mime_type: str) -> None:
        normalized = mime_type.lower().split(";", 1)[0].strip()
        if not normalized or not any(
            normalized.startswith(item) for item in self.policy.allowed_mime_prefixes
        ):
            raise MediaError(f"media MIME type is not allowed: {mime_type}")

    def _path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=min(30, self.policy.timeout_seconds))
        db.row_factory = sqlite3.Row
        return db


class Transcriber(Protocol):
    @property
    def identity(self) -> str: ...
    async def transcribe(self, asset: MediaAsset, *, language: str | None = None) -> str: ...


class TranscriptionService:
    def __init__(self, database: Path) -> None:
        self.database = database
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS gateway_transcripts(
                sha256 TEXT NOT NULL,engine TEXT NOT NULL,language TEXT NOT NULL,
                text TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(sha256,engine,language))"""
            )

    async def transcribe(
        self, asset: MediaAsset, engine: Transcriber, *, language: str | None = None
    ) -> str:
        if not asset.mime_type.startswith("audio/") and not asset.mime_type.startswith("video/"):
            raise MediaError("transcription requires audio or video")
        key_language = language or ""
        with self._connect() as db:
            row = db.execute(
                "SELECT text FROM gateway_transcripts WHERE sha256=? AND engine=? AND language=?",
                (asset.sha256, engine.identity, key_language),
            ).fetchone()
        if row is not None:
            return str(row["text"])
        text = (await engine.transcribe(asset, language=language)).strip()
        if not text:
            raise MediaError("transcriber returned empty text")
        if len(text) > 200_000:
            raise MediaError("transcript exceeds text limit")
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO gateway_transcripts VALUES(?,?,?,?,?)",
                (asset.sha256, engine.identity, key_language, text, datetime.now(UTC).isoformat()),
            )
        return text

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db


class SpeechAPIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str = Field(pattern=r"^https://")
    token_ref: str
    model: str = Field(default="whisper-1", min_length=1, max_length=128)
    timeout_seconds: float = Field(default=60, gt=0, le=300)

    @field_validator("token_ref")
    @classmethod
    def secret_reference(cls, value: str) -> str:
        if not is_reference(value):
            raise ValueError("speech API token must use a secret reference")
        return value


class SpeechAPITranscriber:
    def __init__(self, config: SpeechAPIConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    @property
    def identity(self) -> str:
        return f"speech-api:{self.config.endpoint}:{self.config.model}"

    async def transcribe(self, asset: MediaAsset, *, language: str | None = None) -> str:
        token = resolve_secret(self.config.token_ref)
        if not token:
            raise MediaError("speech API token did not resolve")
        data = {"model": self.config.model}
        if language:
            data["language"] = language
        with asset.path.open("rb") as stream:
            response = await self.client.post(
                self.config.endpoint,
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                files={"file": (asset.name or asset.sha256, stream, asset.mime_type)},
            )
        response.raise_for_status()
        payload: Any = response.json()
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise MediaError("speech API response omitted transcript text")
        return text


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


async def bytes_chunks(data: bytes, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]
