"""Strict channel-neutral gateway contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContentKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ContentKind
    text: str | None = Field(default=None, max_length=200_000)
    uri: str | None = Field(default=None, max_length=4096)
    mime_type: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=512)
    size_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_content(self) -> ContentPart:
        if self.kind is ContentKind.TEXT and not self.text:
            raise ValueError("text content requires text")
        if self.kind is not ContentKind.TEXT and not self.uri:
            raise ValueError("media content requires a URI")
        return self


class ChannelAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    account: str = Field(min_length=1, max_length=256)
    conversation: str = Field(min_length=1, max_length=512)
    thread: str | None = Field(default=None, max_length=512)


class InboundEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str = Field(min_length=1, max_length=512)
    source: ChannelAddress
    sender_id: str = Field(min_length=1, max_length=512)
    content: list[ContentPart] = Field(min_length=1, max_length=64)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reply_to: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def text(self) -> str:
        return "\n".join(part.text or "" for part in self.content if part.kind is ContentKind.TEXT)


class OutboundEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    destination: ChannelAddress
    content: list[ContentPart] = Field(min_length=1, max_length=64)
    reply_to: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboundDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    destination: ChannelAddress
    content: list[ContentPart] = Field(min_length=1, max_length=64)
    reply_to: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"
    CANCELLED = "cancelled"


class GatewayRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: Literal["inbox", "outbox"]
    channel: str
    status: DeliveryState
    attempts: int
    created_at: datetime
    updated_at: datetime
    error: str = ""
