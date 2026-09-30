"""Production Telegram, Discord, and Slack adapters for the neutral gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unknown_agent.config.secrets import is_reference, resolve_secret
from unknown_agent.gateway.contracts import (
    ChannelAddress,
    ContentKind,
    ContentPart,
    InboundEnvelope,
    OutboundEnvelope,
)


def _secret(reference: str) -> str:
    value = resolve_secret(reference)
    if not value:
        raise ValueError(f"secret reference did not resolve: {reference}")
    return value


class _AdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account: str = Field(min_length=1, max_length=256)
    token_ref: str

    @field_validator("token_ref")
    @classmethod
    def reference_only(cls, value: str) -> str:
        if not is_reference(value):
            raise ValueError("adapter tokens must use a secret reference")
        return value


class TelegramConfig(_AdapterConfig):
    webhook_secret_ref: str
    api_base: str = "https://api.telegram.org"

    @field_validator("webhook_secret_ref")
    @classmethod
    def webhook_reference_only(cls, value: str) -> str:
        if not is_reference(value):
            raise ValueError("webhook secrets must use a secret reference")
        return value


class SlackConfig(_AdapterConfig):
    signing_secret_ref: str
    api_base: str = "https://slack.com/api"

    @field_validator("signing_secret_ref")
    @classmethod
    def signing_reference_only(cls, value: str) -> str:
        if not is_reference(value):
            raise ValueError("signing secrets must use a secret reference")
        return value


class DiscordConfig(_AdapterConfig):
    public_key: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    api_base: str = "https://discord.com/api/v10"


class TelegramAdapter:
    def __init__(self, config: TelegramConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=30)

    def normalize(self, body: bytes, headers: dict[str, str]) -> InboundEnvelope | None:
        supplied = headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(supplied, _secret(self.config.webhook_secret_ref)):
            raise PermissionError("invalid Telegram webhook secret")
        payload = json.loads(body)
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict) or message.get("from", {}).get("is_bot"):
            return None
        chat = message.get("chat", {})
        parts = _telegram_parts(message)
        if not parts:
            return None
        return InboundEnvelope(
            external_id=str(payload["update_id"]),
            source=ChannelAddress(
                channel="telegram",
                account=self.config.account,
                conversation=str(chat["id"]),
                thread=str(message["message_thread_id"])
                if message.get("message_thread_id")
                else None,
            ),
            sender_id=str(message["from"]["id"]),
            content=parts,
            timestamp=datetime.fromtimestamp(message.get("date", time.time()), UTC),
            reply_to=(
                str(message["reply_to_message"]["message_id"])
                if message.get("reply_to_message")
                else None
            ),
        )

    async def send(self, envelope: OutboundEnvelope) -> dict[str, Any]:
        text = _text(envelope)
        response = await self.client.post(
            f"{self.config.api_base}/bot{_secret(self.config.token_ref)}/sendMessage",
            json={
                "chat_id": envelope.destination.conversation,
                "text": text,
                **(
                    {"message_thread_id": envelope.destination.thread}
                    if envelope.destination.thread
                    else {}
                ),
                **(
                    {"reply_parameters": {"message_id": envelope.reply_to}}
                    if envelope.reply_to
                    else {}
                ),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(
                f"Telegram rejected message: {payload.get('description', 'unknown')}"
            )
        return {"external_id": str(payload["result"]["message_id"])}


class SlackAdapter:
    def __init__(self, config: SlackConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=30)

    def normalize(self, body: bytes, headers: dict[str, str]) -> InboundEnvelope | None:
        timestamp = headers.get("x-slack-request-timestamp", "")
        try:
            sent_at = int(timestamp)
        except ValueError as exc:
            raise PermissionError("invalid Slack timestamp") from exc
        if abs(int(time.time()) - sent_at) > 300:
            raise PermissionError("expired Slack request")
        base = b"v0:" + timestamp.encode() + b":" + body
        expected = (
            "v0="
            + hmac.new(
                _secret(self.config.signing_secret_ref).encode(), base, hashlib.sha256
            ).hexdigest()
        )
        if not hmac.compare_digest(expected, headers.get("x-slack-signature", "")):
            raise PermissionError("invalid Slack signature")
        payload = json.loads(body)
        event = payload.get("event")
        if (
            not isinstance(event, dict)
            or event.get("type") != "message"
            or event.get("bot_id")
            or event.get("subtype")
        ):
            return None
        return InboundEnvelope(
            external_id=str(payload.get("event_id") or event["ts"]),
            source=ChannelAddress(
                channel="slack",
                account=self.config.account,
                conversation=str(event["channel"]),
                thread=str(event.get("thread_ts")) if event.get("thread_ts") else None,
            ),
            sender_id=str(event["user"]),
            content=[ContentPart(kind=ContentKind.TEXT, text=str(event.get("text", "")))],
            timestamp=datetime.fromtimestamp(float(event.get("ts", sent_at)), UTC),
            reply_to=str(event.get("thread_ts")) if event.get("thread_ts") else None,
        )

    async def send(self, envelope: OutboundEnvelope) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.config.api_base}/chat.postMessage",
            headers={"Authorization": f"Bearer {_secret(self.config.token_ref)}"},
            json={
                "channel": envelope.destination.conversation,
                "text": _text(envelope),
                **(
                    {"thread_ts": envelope.destination.thread}
                    if envelope.destination.thread
                    else {}
                ),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack rejected message: {payload.get('error', 'unknown')}")
        return {"external_id": str(payload["ts"])}


class DiscordAdapter:
    def __init__(self, config: DiscordConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=30)

    def normalize(self, body: bytes, headers: dict[str, str]) -> InboundEnvelope | None:
        signature = headers.get("x-signature-ed25519", "")
        timestamp = headers.get("x-signature-timestamp", "")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.config.public_key)).verify(
                bytes.fromhex(signature), timestamp.encode() + body
            )
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise PermissionError("invalid Discord signature") from exc
        payload = json.loads(body)
        if payload.get("type") == 1:
            return None
        member = payload.get("member", {}).get("user") or payload.get("user") or {}
        text = _discord_text(payload)
        if not text or member.get("bot"):
            return None
        return InboundEnvelope(
            external_id=str(payload["id"]),
            source=ChannelAddress(
                channel="discord",
                account=self.config.account,
                conversation=str(payload["channel_id"]),
                thread=str(payload.get("message", {}).get("thread", {}).get("id"))
                if payload.get("message", {}).get("thread")
                else None,
            ),
            sender_id=str(member["id"]),
            content=[ContentPart(kind=ContentKind.TEXT, text=text)],
            timestamp=datetime.now(UTC),
        )

    async def send(self, envelope: OutboundEnvelope) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.config.api_base}/channels/{envelope.destination.conversation}/messages",
            headers={"Authorization": f"Bot {_secret(self.config.token_ref)}"},
            json={
                "content": _text(envelope),
                **(
                    {"message_reference": {"message_id": envelope.reply_to}}
                    if envelope.reply_to
                    else {}
                ),
            },
        )
        response.raise_for_status()
        return {"external_id": str(response.json()["id"])}


def _text(envelope: OutboundEnvelope) -> str:
    text = "\n".join(
        part.text or "" for part in envelope.content if part.kind is ContentKind.TEXT
    ).strip()
    if not text:
        raise ValueError("adapter currently requires outbound text content")
    return text[:40_000]


def _telegram_parts(message: dict[str, Any]) -> list[ContentPart]:
    parts: list[ContentPart] = []
    text = message.get("text") or message.get("caption")
    if text:
        parts.append(ContentPart(kind=ContentKind.TEXT, text=str(text)))
    media = (("photo", "image"), ("audio", "audio"), ("video", "video"), ("document", "file"))
    for key, kind in media:
        item = message.get(key)
        if isinstance(item, list):
            item = item[-1] if item else None
        if isinstance(item, dict) and item.get("file_id"):
            parts.append(
                ContentPart(
                    kind=ContentKind(kind),
                    uri=f"telegram-file:{item['file_id']}",
                    mime_type=item.get("mime_type"),
                    name=item.get("file_name"),
                    size_bytes=item.get("file_size"),
                )
            )
    return parts


def _discord_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("content"), str):
        return payload["content"].strip()
    data = payload.get("data") or {}
    pieces = [str(data.get("name", ""))]
    pieces.extend(
        str(item.get("value", "")) for item in data.get("options", []) if isinstance(item, dict)
    )
    return " ".join(piece for piece in pieces if piece).strip()
