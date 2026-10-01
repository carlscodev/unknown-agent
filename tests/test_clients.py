from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from unknown_agent.clients import (
    ClientConfig,
    ClientError,
    ClientEvent,
    ClientRuntime,
    ClientSyncService,
    ClientTransport,
    SecureClientStore,
    SyncRequest,
    SyncResponse,
)


def keys(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    signing = Ed25519PrivateKey.generate()
    raw = signing.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv("CLIENT_SIGNING", raw.hex())
    monkeypatch.setenv("CLIENT_STORAGE", bytes(range(32)).hex())
    return signing


def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SecureClientStore:
    keys(monkeypatch)
    return SecureClientStore(tmp_path / "client.db", "env:CLIENT_STORAGE")


def test_offline_queue_is_encrypted_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = store(tmp_path, monkeypatch)
    operation = local.queue("message.send", {"text": "highly secret message"}, operation_id="op-1")
    assert (
        local.queue("message.send", {"text": "highly secret message"}, operation_id="op-1").id
        == operation.id
    )
    with sqlite3.connect(tmp_path / "client.db") as db:
        ciphertext = db.execute(
            "SELECT ciphertext FROM client_operations WHERE id='op-1'"
        ).fetchone()[0]
    assert b"highly secret message" not in ciphertext
    assert local.pending()[0].payload["text"] == "highly secret message"
    with pytest.raises(ClientError, match="different content"):
        local.queue("message.send", {"text": "changed"}, operation_id="op-1")


def test_sync_application_is_atomic_deduplicated_and_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = store(tmp_path, monkeypatch)
    local.queue("message.send", {"text": "outgoing"}, operation_id="op-1")
    event = ClientEvent(
        id="event-1",
        sequence=1,
        kind="message",
        payload={"text": "incoming"},
        created_at=datetime.now(UTC),
    )
    response = SyncResponse(
        cursor="cursor-1", events=(event,), acknowledged_operation_ids=("op-1",)
    )
    local.apply(response)
    local.apply(response)
    assert local.cursor == "cursor-1"
    assert local.pending() == ()
    assert local.events()[0].payload == {"text": "incoming"}
    with sqlite3.connect(tmp_path / "client.db") as db:
        blob = bytearray(
            db.execute("SELECT ciphertext FROM client_events WHERE id='event-1'").fetchone()[0]
        )
        blob[-1] ^= 1
        db.execute("UPDATE client_events SET ciphertext=? WHERE id='event-1'", (bytes(blob),))
    with pytest.raises(ClientError, match="authentication"):
        local.events()


@pytest.mark.asyncio
async def test_signed_transport_and_runtime_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signing = keys(monkeypatch)
    seen = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        timestamp = int(request.headers["x-device-timestamp"])
        nonce = request.headers["x-device-nonce"]
        digest = __import__("hashlib").sha256(request.content).hexdigest()
        proof = f"unknown-agent-device-request:v1:{timestamp}:{nonce}:{digest}".encode()
        signing.public_key().verify(bytes.fromhex(request.headers["x-device-signature"]), proof)
        payload = json.loads(request.content)
        assert payload["operations"][0]["id"] == "op-1"
        return httpx.Response(
            200,
            json={
                "cursor": "next",
                "events": [],
                "acknowledged_operation_ids": ["op-1"],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ClientConfig(
        endpoint="https://agent.example",
        device_id="device-1",
        signing_key_ref="env:CLIENT_SIGNING",
        storage_key_ref="env:CLIENT_STORAGE",
    )
    local = SecureClientStore(tmp_path / "client.db", config.storage_key_ref)
    local.queue("message.send", {"text": "hello"}, operation_id="op-1")
    runtime = ClientRuntime(local, ClientTransport(config, client=client))
    response = await runtime.sync_once()
    assert response.cursor == "next" and local.cursor == "next" and seen == 1
    assert local.pending() == ()
    await client.aclose()


def test_server_sync_is_durable_idempotent_and_capability_scoped(tmp_path: Path) -> None:
    service = ClientSyncService(tmp_path / "server.db")
    published = service.publish("approval", {"action": "deploy"}, target_device="device-1")
    operation = {
        "id": "approval:one",
        "kind": "approval.resolve",
        "payload": {"approval_id": "one", "approved": True, "reason": "reviewed"},
        "created_at": datetime.now(UTC).isoformat(),
    }
    request = SyncRequest.model_validate({"cursor": "", "operations": [operation]})
    first = service.sync("device-1", ("approval.resolve",), request)
    second = service.sync("device-1", ("approval.resolve",), request)
    assert first.events[0].id == published.id
    assert first.acknowledged_operation_ids == second.acknowledged_operation_ids
    assert service.approval("one")["approved"] == 1
    with pytest.raises(ClientError, match="lacks capability"):
        service.sync("device-2", (), request)


def test_remote_approval_and_security_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = store(tmp_path, monkeypatch)
    approval = local.resolve_approval("approval-1", approved=False, reason="unsafe")
    assert approval.payload == {
        "approval_id": "approval-1",
        "approved": False,
        "reason": "unsafe",
    }
    with pytest.raises(ClientError, match="1 MiB"):
        local.queue("message.send", {"text": "x" * 1_048_577})
    with pytest.raises(ValidationError):
        ClientConfig(
            endpoint="http://insecure.example",
            device_id="device",
            signing_key_ref="raw",
            storage_key_ref="raw",
            timeout_seconds=301,
        )
