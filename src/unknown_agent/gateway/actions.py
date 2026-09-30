"""Gateway routes that bridge inbound messages to the canonical agent runner."""

from __future__ import annotations

from collections.abc import Callable

from unknown_agent.config.loader import LoadedSettings
from unknown_agent.core.runner import RunRequest, run_task
from unknown_agent.gateway.contracts import ContentKind, ContentPart, InboundEnvelope, OutboundDraft
from unknown_agent.gateway.service import Gateway


def register_agent_route(
    gateway: Gateway,
    loaded_factory: Callable[[], LoadedSettings],
) -> None:
    """Register the channel-neutral production agent route."""

    async def agent(message: InboundEnvelope) -> list[OutboundDraft]:
        task = message.text().strip()
        if not task:
            return [
                OutboundDraft(
                    destination=message.source,
                    content=[
                        ContentPart(kind=ContentKind.TEXT, text="A text instruction is required.")
                    ],
                    reply_to=message.external_id,
                )
            ]
        result = await run_task(
            loaded_factory(),
            RunRequest(
                task=task,
                yes=False,
                non_interactive=True,
                metadata={
                    "gateway_channel": message.source.channel,
                    "gateway_account": message.source.account,
                    "gateway_conversation": message.source.conversation,
                    "gateway_sender": message.sender_id,
                    "gateway_external_id": message.external_id,
                },
            ),
        )
        text = result.summary or (
            "Mission completed." if result.ok else "Mission stopped without a summary."
        )
        return [
            OutboundDraft(
                destination=message.source,
                content=[ContentPart(kind=ContentKind.TEXT, text=text)],
                reply_to=message.external_id,
                metadata={"run_id": result.run_id, "status": result.status.value},
            )
        ]

    gateway.register_route("*", agent, source="unknown-agent:built-in")
