from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from unknown_agent.gateway import ContentPart
from unknown_agent.gateway.media import (
    MediaError,
    MediaPolicy,
    MediaStore,
    ResolvedMedia,
    SpeechAPIConfig,
    SpeechAPITranscriber,
    TranscriptionService,
    bytes_chunks,
)


@pytest.mark.asyncio
async def test_media_store_deduplicates_and_verifies_integrity(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "media", tmp_path / "gateway.db")
    data = b"durable audio bytes"
    first = await store.ingest(bytes_chunks(data), mime_type="audio/ogg", name="voice.ogg")
    second = await store.ingest(
        bytes_chunks(data),
        mime_type="audio/ogg",
        declared_size=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == data
    first.path.write_bytes(b"tampered")
    with pytest.raises(MediaError, match="integrity"):
        store.get(first.sha256)


@pytest.mark.asyncio
async def test_media_limits_and_declared_integrity(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "media", tmp_path / "gateway.db", MediaPolicy(max_bytes=4))
    with pytest.raises(MediaError, match="exceeds"):
        await store.ingest(bytes_chunks(b"12345"), mime_type="audio/ogg")
    with pytest.raises(MediaError, match="declaration"):
        await store.ingest(bytes_chunks(b"123"), mime_type="audio/ogg", declared_size=4)
    with pytest.raises(MediaError, match="MIME"):
        await store.ingest(bytes_chunks(b"x"), mime_type="application/x-executable")


@pytest.mark.asyncio
async def test_registered_resolver_hydrates_neutral_part(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "media", tmp_path / "gateway.db")

    async def resolve(uri: str) -> ResolvedMedia:
        assert uri == "telegram-file:abc"
        return ResolvedMedia(
            chunks=bytes_chunks(b"image"),
            mime_type="image/jpeg",
            name="photo.jpg",
            size_bytes=5,
        )

    store.register_resolver("telegram-file", resolve)
    part, asset = await store.hydrate(ContentPart(kind="image", uri="telegram-file:abc"))
    assert part.uri == f"cas:{asset.sha256}"
    assert part.sha256 == asset.sha256 and part.size_bytes == 5


@pytest.mark.asyncio
async def test_speech_api_transcription_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPEECH_TOKEN", "secret")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"text": " hello world "})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = MediaStore(tmp_path / "media", tmp_path / "gateway.db")
    asset = await store.ingest(bytes_chunks(b"audio"), mime_type="audio/wav")
    engine = SpeechAPITranscriber(
        SpeechAPIConfig(
            endpoint="https://speech.example/v1/transcriptions",
            token_ref="env:SPEECH_TOKEN",
        ),
        client=client,
    )
    service = TranscriptionService(tmp_path / "gateway.db")
    assert await service.transcribe(asset, engine, language="en") == "hello world"
    assert await service.transcribe(asset, engine, language="en") == "hello world"
    assert calls == 1
    await client.aclose()


def test_media_and_speech_timeouts_are_bounded() -> None:
    with pytest.raises(ValidationError):
        MediaPolicy(timeout_seconds=301)
    with pytest.raises(ValidationError, match="reference"):
        SpeechAPIConfig(endpoint="https://speech.example", token_ref="raw")
