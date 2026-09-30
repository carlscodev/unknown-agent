from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from unknown_agent.gateway import ChannelAddress, ContentPart, OutboundEnvelope
from unknown_agent.gateway.mobile_adapters import (
    MatrixAdapter,
    MatrixConfig,
    WhatsAppAdapter,
    WhatsAppConfig,
)


def whatsapp(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient | None = None
) -> WhatsAppAdapter:
    monkeypatch.setenv("WA_TOKEN", "access")
    monkeypatch.setenv("WA_APP", "application-secret")
    monkeypatch.setenv("WA_VERIFY", "verification-secret")
    return WhatsAppAdapter(
        WhatsAppConfig(
            account="business",
            phone_number_id="123",
            token_ref="env:WA_TOKEN",
            app_secret_ref="env:WA_APP",
            verify_token_ref="env:WA_VERIFY",
        ),
        client=client,
    )


def test_whatsapp_challenge_and_signed_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = whatsapp(monkeypatch)
    assert adapter.verify_challenge("subscribe", "verification-secret", "challenge") == "challenge"
    with pytest.raises(PermissionError):
        adapter.verify_challenge("subscribe", "wrong", "challenge")
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "123"},
                            "messages": [
                                {
                                    "id": "wamid.1",
                                    "from": "15551234567",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                },
                                {
                                    "id": "wamid.2",
                                    "from": "15551234567",
                                    "type": "image",
                                    "image": {"id": "media-1", "mime_type": "image/jpeg"},
                                },
                            ],
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"application-secret", body, hashlib.sha256).hexdigest()
    messages = adapter.normalize(body, {"x-hub-signature-256": signature})
    assert [item.external_id for item in messages] == ["wamid.1", "wamid.2"]
    assert messages[0].text() == "hello"
    assert messages[1].content[0].uri == "whatsapp-media:media-1"
    with pytest.raises(PermissionError):
        adapter.normalize(body, {"x-hub-signature-256": "sha256=bad"})


@pytest.mark.asyncio
async def test_whatsapp_send(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access"
        assert request.url.path.endswith("/v23.0/123/messages")
        return httpx.Response(200, json={"messages": [{"id": "wamid.sent"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = whatsapp(monkeypatch, client)
    envelope = OutboundEnvelope(
        id="out-1",
        destination=ChannelAddress(
            channel="whatsapp", account="business", conversation="15551234567"
        ),
        content=[ContentPart(kind="text", text="reply")],
    )
    assert await adapter.send(envelope) == {"external_id": "wamid.sent"}
    await client.aclose()


def matrix(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient | None = None
) -> MatrixAdapter:
    monkeypatch.setenv("MATRIX_TOKEN", "matrix-access")
    return MatrixAdapter(
        MatrixConfig(
            account="agent",
            homeserver="https://matrix.example",
            token_ref="env:MATRIX_TOKEN",
            user_id="@agent:example",
        ),
        client=client,
    )


def test_matrix_normalizes_timeline_and_ignores_self(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = matrix(monkeypatch)
    payload = {
        "next_batch": "s2",
        "rooms": {
            "join": {
                "!room:example": {
                    "timeline": {
                        "events": [
                            {
                                "type": "m.room.message",
                                "event_id": "$1",
                                "sender": "@user:example",
                                "origin_server_ts": 1700000000000,
                                "content": {"msgtype": "m.text", "body": "hello"},
                            },
                            {
                                "type": "m.room.message",
                                "event_id": "$2",
                                "sender": "@agent:example",
                                "content": {"msgtype": "m.text", "body": "self"},
                            },
                        ]
                    }
                }
            }
        },
    }
    messages = adapter.normalize_sync(payload)
    assert len(messages) == 1
    assert messages[0].external_id == "$1" and messages[0].text() == "hello"


@pytest.mark.asyncio
async def test_matrix_sync_and_send(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer matrix-access"
        if request.method == "GET":
            assert request.url.params["since"] == "s1"
            return httpx.Response(200, json={"next_batch": "s2", "rooms": {"join": {}}})
        assert "%21room%3Aexample" in str(request.url)
        return httpx.Response(200, json={"event_id": "$sent"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = matrix(monkeypatch, client)
    messages, cursor = await adapter.sync("s1", timeout_ms=25000)
    assert messages == [] and cursor == "s2"
    envelope = OutboundEnvelope(
        id="out-2",
        destination=ChannelAddress(channel="matrix", account="agent", conversation="!room:example"),
        content=[ContentPart(kind="text", text="reply")],
    )
    assert await adapter.send(envelope) == {"external_id": "$sent"}
    with pytest.raises(ValueError, match="300000"):
        await adapter.sync(timeout_ms=300001)
    await client.aclose()


def test_mobile_configs_reject_raw_tokens() -> None:
    with pytest.raises(ValidationError, match="reference"):
        MatrixConfig(
            account="agent",
            homeserver="https://matrix.example",
            token_ref="raw",
            user_id="@agent:example",
        )
