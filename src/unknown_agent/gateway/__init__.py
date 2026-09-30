"""Durable channel-neutral messaging gateway."""

from unknown_agent.gateway.actions import register_agent_route
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
    "ChannelAddress",
    "ContentKind",
    "ContentPart",
    "DeliveryState",
    "Gateway",
    "GatewayRecord",
    "InboundEnvelope",
    "OutboundDraft",
    "OutboundEnvelope",
    "register_agent_route",
]
