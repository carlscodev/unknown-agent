"""Durable channel-neutral messaging gateway."""

from unknown_agent.gateway.access import AccessController, AccessDeniedError, Identity, PairingCode
from unknown_agent.gateway.actions import register_agent_route
from unknown_agent.gateway.adapters import (
    DiscordAdapter,
    DiscordConfig,
    SlackAdapter,
    SlackConfig,
    TelegramAdapter,
    TelegramConfig,
)
from unknown_agent.gateway.contracts import (
    ChannelAddress,
    ContentKind,
    ContentPart,
    DeliveryState,
    GatewayRecord,
    InboundEnvelope,
    OutboundDraft,
    OutboundEnvelope,
)
from unknown_agent.gateway.devices import (
    Device,
    DeviceCommand,
    DeviceController,
    DeviceError,
    Enrollment,
)
from unknown_agent.gateway.media import (
    MediaAsset,
    MediaError,
    MediaPolicy,
    MediaStore,
    ResolvedMedia,
    SpeechAPIConfig,
    SpeechAPITranscriber,
    TranscriptionService,
)
from unknown_agent.gateway.mobile_adapters import (
    MatrixAdapter,
    MatrixConfig,
    WhatsAppAdapter,
    WhatsAppConfig,
)
from unknown_agent.gateway.service import Gateway

__all__ = [
    "AccessController",
    "AccessDeniedError",
    "ChannelAddress",
    "ContentKind",
    "ContentPart",
    "DeliveryState",
    "Device",
    "DeviceCommand",
    "DeviceController",
    "DeviceError",
    "DiscordAdapter",
    "DiscordConfig",
    "Enrollment",
    "Gateway",
    "GatewayRecord",
    "Identity",
    "InboundEnvelope",
    "MatrixAdapter",
    "MatrixConfig",
    "MediaAsset",
    "MediaError",
    "MediaPolicy",
    "MediaStore",
    "OutboundDraft",
    "OutboundEnvelope",
    "PairingCode",
    "ResolvedMedia",
    "SlackAdapter",
    "SlackConfig",
    "SpeechAPIConfig",
    "SpeechAPITranscriber",
    "TelegramAdapter",
    "TelegramConfig",
    "TranscriptionService",
    "WhatsAppAdapter",
    "WhatsAppConfig",
    "register_agent_route",
]
