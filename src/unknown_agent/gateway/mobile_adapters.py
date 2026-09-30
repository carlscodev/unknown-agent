"""WhatsApp Cloud API and Matrix client-server gateway adapters."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
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


def _require_reference(value: str) -> str:
    if not is_reference(value):
        raise ValueError("credentials must use a secret reference")
    return value


class WhatsAppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account: str = Field(min_length=1, max_length=256)
    phone_number_id: str = Field(min_length=1, max_length=128)
    token_ref: str
    app_secret_ref: str
    verify_token_ref: str
    api_version: str = Field(default="v23.0", pattern=r"^v\d+\.\d+$")
    api_base: str = "https://graph.facebook.com"

    _token_reference = field_validator("token_ref")(_require_reference)
    _app_reference = field_validator("app_secret_ref")(_require_reference)
    _verify_reference = field_validator("verify_token_ref")(_require_reference)


class MatrixConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account: str = Field(min_length=1, max_length=256)
    homeserver: str = Field(pattern=r"^https://")
    token_ref: str
    user_id: str = Field(pattern=r"^@[^:]+:.+$")
    request_timeout: float = Field(default=30.0, gt=0, le=300)

    _token_reference = field_validator("token_ref")(_require_reference)


class WhatsAppAdapter:
    def __init__(self, config: WhatsAppConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=30)

    def verify_challenge(self, mode: str, token: str, challenge: str) -> str:
        if mode != "subscribe" or not hmac.compare_digest(
            token, _secret(self.config.verify_token_ref)
        ):
            raise PermissionError("invalid WhatsApp verification token")
        return challenge

    def normalize(self, body: bytes, headers: dict[str, str]) -> list[InboundEnvelope]:
        supplied = headers.get("x-hub-signature-256", "")
        expected = (
            "sha256="
            + hmac.new(
                _secret(self.config.app_secret_ref).encode(), body, hashlib.sha256
            ).hexdigest()
        )
        if not hmac.compare_digest(supplied, expected):
            raise PermissionError("invalid WhatsApp webhook signature")
        payload = json.loads(body)
        envelopes: list[InboundEnvelope] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata", {})
                if str(metadata.get("phone_number_id", "")) != self.config.phone_number_id:
                    continue
                for message in value.get("messages", []):
                    envelope = self._message(message)
                    if envelope is not None:
                        envelopes.append(envelope)
        return envelopes

    def _message(self, message: dict[str, Any]) -> InboundEnvelope | None:
        parts = _whatsapp_parts(message)
        if not parts or not message.get("from") or not message.get("id"):
            return None
        sender = str(message["from"])
        context = message.get("context", {})
        stamp = message.get("timestamp")
        return InboundEnvelope(
            external_id=str(message["id"]),
            source=ChannelAddress(
                channel="whatsapp",
                account=self.config.account,
                conversation=sender,
            ),
            sender_id=sender,
            content=parts,
            timestamp=datetime.fromtimestamp(float(stamp), UTC) if stamp else datetime.now(UTC),
            reply_to=str(context["id"]) if context.get("id") else None,
        )

    async def send(self, envelope: OutboundEnvelope) -> dict[str, Any]:
        text = _outbound_text(envelope, limit=4096)
        response = await self.client.post(
            f"{self.config.api_base}/{self.config.api_version}/{self.config.phone_number_id}/messages",
            headers={"Authorization": f"Bearer {_secret(self.config.token_ref)}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": envelope.destination.conversation,
                "type": "text",
                "text": {"preview_url": False, "body": text},
                **({"context": {"message_id": envelope.reply_to}} if envelope.reply_to else {}),
            },
        )
        response.raise_for_status()
        payload = response.json()
        messages = payload.get("messages", [])
        if not messages or not messages[0].get("id"):
            raise RuntimeError("WhatsApp response omitted a message id")
        return {"external_id": str(messages[0]["id"])}


class MatrixAdapter:
    def __init__(self, config: MatrixConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=config.request_timeout)

    async def sync(
        self, since: str | None = None, *, timeout_ms: int = 30_000
    ) -> tuple[list[InboundEnvelope], str]:
        if not 0 <= timeout_ms <= 300_000:
            raise ValueError("Matrix sync timeout must be between 0 and 300000 milliseconds")
        params: dict[str, Any] = {"timeout": timeout_ms}
        if since:
            params["since"] = since
        response = await self.client.get(
            f"{self.config.homeserver}/_matrix/client/v3/sync",
            headers=self._headers(),
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        return self.normalize_sync(payload), str(payload["next_batch"])

    def normalize_sync(self, payload: dict[str, Any]) -> list[InboundEnvelope]:
        envelopes: list[InboundEnvelope] = []
        joined = payload.get("rooms", {}).get("join", {})
        for room_id, room in joined.items():
            for event in room.get("timeline", {}).get("events", []):
                envelope = self._event(str(room_id), event)
                if envelope is not None:
                    envelopes.append(envelope)
        return envelopes

    def _event(self, room_id: str, event: dict[str, Any]) -> InboundEnvelope | None:
        if event.get("type") != "m.room.message" or event.get("sender") == self.config.user_id:
            return None
        content = event.get("content", {})
        parts = _matrix_parts(content)
        if not parts or not event.get("event_id") or not event.get("sender"):
            return None
        relates = content.get("m.relates_to", {})
        reply = relates.get("m.in_reply_to", {}).get("event_id")
        timestamp = event.get("origin_server_ts")
        return InboundEnvelope(
            external_id=str(event["event_id"]),
            source=ChannelAddress(
                channel="matrix",
                account=self.config.account,
                conversation=room_id,
                thread=str(relates["event_id"]) if relates.get("rel_type") == "m.thread" else None,
            ),
            sender_id=str(event["sender"]),
            content=parts,
            timestamp=(
                datetime.fromtimestamp(float(timestamp) / 1000, UTC)
                if timestamp is not None
                else datetime.now(UTC)
            ),
            reply_to=str(reply) if reply else None,
        )

    async def send(self, envelope: OutboundEnvelope) -> dict[str, Any]:
        transaction = secrets.token_urlsafe(18)
        room = quote(envelope.destination.conversation, safe="")
        body: dict[str, Any] = {"msgtype": "m.text", "body": _outbound_text(envelope, limit=65_536)}
        if envelope.reply_to:
            body["m.relates_to"] = {"m.in_reply_to": {"event_id": envelope.reply_to}}
        response = await self.client.put(
            f"{self.config.homeserver}/_matrix/client/v3/rooms/{room}/send/m.room.message/{transaction}",
            headers=self._headers(),
            json=body,
        )
        response.raise_for_status()
        event_id = response.json().get("event_id")
        if not event_id:
            raise RuntimeError("Matrix response omitted an event id")
        return {"external_id": str(event_id)}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {_secret(self.config.token_ref)}"}


def _outbound_text(envelope: OutboundEnvelope, *, limit: int) -> str:
    text = "\n".join(
        part.text or "" for part in envelope.content if part.kind is ContentKind.TEXT
    ).strip()
    if not text:
        raise ValueError("adapter currently requires outbound text content")
    return text[:limit]


def _whatsapp_parts(message: dict[str, Any]) -> list[ContentPart]:
    kind = message.get("type")
    if kind == "text" and message.get("text", {}).get("body"):
        return [ContentPart(kind=ContentKind.TEXT, text=str(message["text"]["body"]))]
    mapping = {
        "image": ContentKind.IMAGE,
        "audio": ContentKind.AUDIO,
        "video": ContentKind.VIDEO,
        "document": ContentKind.FILE,
        "sticker": ContentKind.IMAGE,
    }
    media = message.get(str(kind), {})
    if kind in mapping and media.get("id"):
        parts: list[ContentPart] = []
        if media.get("caption"):
            parts.append(ContentPart(kind=ContentKind.TEXT, text=str(media["caption"])))
        parts.append(
            ContentPart(
                kind=mapping[kind],
                uri=f"whatsapp-media:{media['id']}",
                mime_type=media.get("mime_type"),
                name=media.get("filename"),
                sha256=media.get("sha256"),
            )
        )
        return parts
    return []


def _matrix_parts(content: dict[str, Any]) -> list[ContentPart]:
    msgtype = content.get("msgtype")
    if msgtype in {"m.text", "m.notice", "m.emote"} and content.get("body"):
        return [ContentPart(kind=ContentKind.TEXT, text=str(content["body"]))]
    mapping = {
        "m.image": ContentKind.IMAGE,
        "m.audio": ContentKind.AUDIO,
        "m.video": ContentKind.VIDEO,
        "m.file": ContentKind.FILE,
    }
    if msgtype in mapping and content.get("url"):
        info = content.get("info", {})
        return [
            ContentPart(
                kind=mapping[msgtype],
                uri=str(content["url"]),
                mime_type=info.get("mimetype"),
                name=content.get("body"),
                size_bytes=info.get("size"),
            )
        ]
    return []
