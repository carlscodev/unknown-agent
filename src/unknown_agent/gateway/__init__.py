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
from unknown_agent.gateway.service import Gateway

__all__ = [
    "AccessController",
    "AccessDeniedError",
    "ChannelAddress",
    "ContentKind",
    "ContentPart",
    "DeliveryState",
    "DiscordAdapter",
    "DiscordConfig",
    "Gateway",
    "GatewayRecord",
    "Identity",
    "InboundEnvelope",
    "OutboundDraft",
    "OutboundEnvelope",
    "PairingCode",
    "SlackAdapter",
    "SlackConfig",
    "TelegramAdapter",
    "TelegramConfig",
    "register_agent_route",
]
