from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from unknown_agent.config.secrets import REDACTOR
from unknown_agent.gateway import (
    ChannelAddress,
    ContentPart,
    Gateway,
    InboundEnvelope,
    OutboundDraft,
)


def inbound(*, external_id: str = "msg-1", text: str = "hello") -> InboundEnvelope:
    return InboundEnvelope(
        external_id=external_id,
        source=ChannelAddress(channel="test", account="bot", conversation="room"),
        sender_id="user-1",
        content=[ContentPart(kind="text", text=text)],
    )


@pytest.mark.asyncio
async def test_inbox_routes_to_transactional_outbox_and_transport(tmp_path: Path) -> None:
    gateway = Gateway(tmp_path / "gateway.db")
    sent: list[str] = []

    async def route(message: InboundEnvelope) -> list[OutboundDraft]:
        return [
            OutboundDraft(
                destination=message.source,
                content=[ContentPart(kind="text", text=f"reply: {message.text()}")],
                reply_to=message.external_id,
            )
        ]

    async def transport(message: object) -> dict[str, str]:
        sent.append(message.content[0].text)  # type: ignore[attr-defined]
        return {"external_id": "reply-1"}

    gateway.register_route("test", route)
    gateway.register_transport("test", transport)
    gateway.receive(inbound())
    records = await gateway.drain()
    assert sent == ["reply: hello"]
    assert {record.status for record in records} == {"succeeded"}
    assert gateway._connect().execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_duplicate_delivery_is_idempotent_and_conflicts_are_rejected(tmp_path: Path) -> None:
    gateway = Gateway(tmp_path / "gateway.db")
    message = inbound()
    first = gateway.receive(message)
    assert gateway.receive(message) == first
    with pytest.raises(ValueError, match="different content"):
        gateway.receive(
            message.model_copy(update={"content": [ContentPart(kind="text", text="changed")]})
        )


def test_secrets_are_redacted_before_persistence(tmp_path: Path) -> None:
    gateway = Gateway(tmp_path / "gateway.db")
    secret = "github_pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    REDACTOR.register(secret)
    identifier = gateway.receive(inbound(text=f"token={secret}"))
    row = gateway._row("gateway_inbox", identifier)
    assert secret not in row["envelope_json"]
    assert "********" in row["envelope_json"]


@pytest.mark.asyncio
async def test_atomic_claim_prevents_duplicate_routes(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    first = Gateway(path, owner="first")
    second = Gateway(path, owner="second")
    calls = 0

    async def route(_message: InboundEnvelope) -> list[OutboundDraft]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return []

    first.register_route("test", route)
    second.register_route("test", route)
    first.receive(inbound())
    await asyncio.gather(first.drain(), second.drain())
    assert calls == 1


@pytest.mark.asyncio
async def test_missing_route_retries_then_dead_letters(tmp_path: Path) -> None:
    gateway = Gateway(tmp_path / "gateway.db")
    identifier = gateway.receive(inbound())
    for _ in range(5):
        with gateway._connect() as db:
            db.execute(
                "UPDATE gateway_inbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (identifier,),
            )
        await gateway.drain()
    row = gateway._row("gateway_inbox", identifier)
    assert row["status"] == "dead"
    assert row["attempts"] == 5
    assert "no route" in row["error"]


@pytest.mark.asyncio
async def test_tampered_envelope_is_contained(tmp_path: Path) -> None:
    gateway = Gateway(tmp_path / "gateway.db")

    async def route(_message: InboundEnvelope) -> list[OutboundDraft]:
        raise AssertionError("tampered input must not run")

    gateway.register_route("test", route)
    identifier = gateway.receive(inbound())
    with gateway._connect() as db:
        db.execute("UPDATE gateway_inbox SET envelope_json='{}' WHERE id=?", (identifier,))
        db.execute("UPDATE gateway_inbox SET attempts=4 WHERE id=?", (identifier,))
    await gateway.drain()
    row = gateway._row("gateway_inbox", identifier)
    assert row["status"] == "dead"
    assert "integrity" in row["error"]


def test_contracts_reject_malformed_media_and_addresses() -> None:
    with pytest.raises(ValidationError, match="requires a URI"):
        ContentPart(kind="image")
    with pytest.raises(ValidationError):
        ChannelAddress(channel="INVALID CHANNEL", account="a", conversation="c")


def test_sync_registrations_and_oversized_envelopes_are_rejected(tmp_path: Path) -> None:
    gateway = Gateway(tmp_path / "gateway.db")
    with pytest.raises(TypeError, match="async"):
        gateway.register_route("test", lambda _message: [])  # type: ignore[arg-type]
    huge = inbound().model_copy(update={"metadata": {"large": "x" * 1_100_000}})
    with pytest.raises(ValueError, match="exceeds"):
        gateway.receive(huge)
