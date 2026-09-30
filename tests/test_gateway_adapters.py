from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from unknown_agent.gateway import ChannelAddress, ContentPart, OutboundEnvelope
from unknown_agent.gateway.adapters import (
    DiscordAdapter,
    DiscordConfig,
    SlackAdapter,
    SlackConfig,
    TelegramAdapter,
    TelegramConfig,
)


def test_telegram_normalizes_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "token")
    monkeypatch.setenv("TG_SECRET", "hook")
    adapter = TelegramAdapter(
        TelegramConfig(account="bot", token_ref="env:TG_TOKEN", webhook_secret_ref="env:TG_SECRET")
    )
    body = json.dumps(
        {
            "update_id": 7,
            "message": {
                "message_id": 2,
                "date": 1,
                "from": {"id": 3},
                "chat": {"id": 4},
                "text": "hello",
            },
        }
    ).encode()
    msg = adapter.normalize(body, {"x-telegram-bot-api-secret-token": "hook"})
    assert msg and msg.external_id == "7" and msg.text() == "hello"
    with pytest.raises(PermissionError):
        adapter.normalize(body, {})


@pytest.mark.asyncio
async def test_telegram_send_uses_secret_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "token")
    monkeypatch.setenv("TG_SECRET", "hook")

    async def handler(request: httpx.Request):
        assert "/bottoken/sendMessage" in str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TelegramAdapter(
        TelegramConfig(account="bot", token_ref="env:TG_TOKEN", webhook_secret_ref="env:TG_SECRET"),
        client=client,
    )
    out = OutboundEnvelope(
        id="1",
        destination=ChannelAddress(channel="telegram", account="bot", conversation="4"),
        content=[ContentPart(kind="text", text="reply")],
    )
    assert (await adapter.send(out))["external_id"] == "9"
    await client.aclose()


def test_slack_signature_replay_and_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SL_TOKEN", "token")
    monkeypatch.setenv("SL_SECRET", "secret")
    now = str(int(time.time()))
    body = json.dumps(
        {
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "hello",
                "ts": now + ".0",
            },
        }
    ).encode()
    sig = (
        "v0=" + hmac.new(b"secret", b"v0:" + now.encode() + b":" + body, hashlib.sha256).hexdigest()
    )
    adapter = SlackAdapter(
        SlackConfig(account="app", token_ref="env:SL_TOKEN", signing_secret_ref="env:SL_SECRET")
    )
    msg = adapter.normalize(body, {"x-slack-request-timestamp": now, "x-slack-signature": sig})
    assert msg and msg.sender_id == "U1"
    with pytest.raises(PermissionError, match="expired"):
        adapter.normalize(body, {"x-slack-request-timestamp": "1", "x-slack-signature": sig})


def test_discord_ed25519_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DC_TOKEN", "token")
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    adapter = DiscordAdapter(
        DiscordConfig(account="app", token_ref="env:DC_TOKEN", public_key=public)
    )
    body = json.dumps(
        {
            "id": "I1",
            "type": 2,
            "channel_id": "C1",
            "member": {"user": {"id": "U1"}},
            "data": {"name": "review", "options": [{"value": "repo"}]},
        }
    ).encode()
    stamp = str(int(time.time()))
    signature = private.sign(stamp.encode() + body).hex()
    msg = adapter.normalize(
        body, {"x-signature-ed25519": signature, "x-signature-timestamp": stamp}
    )
    assert msg and msg.text() == "review repo"
    with pytest.raises(PermissionError):
        adapter.normalize(body, {"x-signature-ed25519": "00" * 64, "x-signature-timestamp": stamp})


def test_adapter_configs_reject_raw_secrets() -> None:
    with pytest.raises(ValidationError, match="reference"):
        TelegramConfig(account="x", token_ref="raw", webhook_secret_ref="raw")
