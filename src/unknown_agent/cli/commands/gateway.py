"""``ua gateway`` — operate the durable channel-neutral inbox and outbox."""

from __future__ import annotations

import asyncio

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.gateway import (
    ChannelAddress,
    ContentKind,
    ContentPart,
    Gateway,
    InboundEnvelope,
    register_agent_route,
)

app = typer.Typer(name="gateway", no_args_is_help=True)


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if not isinstance(runtime, Runtime):
        raise UsageError("CLI runtime is not initialised.")
    return runtime


def _gateway(ctx: typer.Context) -> Gateway:
    loaded = _runtime(ctx).load()
    return Gateway(loaded.paths.project_dir / "gateway.db")


@app.command("receive")
def receive(
    ctx: typer.Context,
    channel: str = typer.Option(..., "--channel"),
    account: str = typer.Option(..., "--account"),
    conversation: str = typer.Option(..., "--conversation"),
    sender: str = typer.Option(..., "--sender"),
    external_id: str = typer.Option(..., "--external-id"),
    text: str = typer.Option(..., "--text"),
    thread: str | None = typer.Option(None, "--thread"),
) -> None:
    """Persist one normalized inbound envelope; useful for adapters and diagnostics."""

    gateway = _gateway(ctx)
    identifier = gateway.receive(
        InboundEnvelope(
            external_id=external_id,
            source=ChannelAddress(
                channel=channel,
                account=account,
                conversation=conversation,
                thread=thread,
            ),
            sender_id=sender,
            content=[ContentPart(kind=ContentKind.TEXT, text=text)],
        )
    )
    _runtime(ctx).emit_json({"id": identifier, "accepted": True})


@app.command("list")
def list_records(
    ctx: typer.Context,
    limit: int = typer.Option(100, "--limit", min=1, max=1000),
) -> None:
    """List durable inbox and outbox records."""

    records = _gateway(ctx).records(limit=limit)
    _runtime(ctx).emit_json({"records": [record.model_dump(mode="json") for record in records]})


@app.command("drain")
def drain(ctx: typer.Context) -> None:
    """Route and send currently due records once."""

    runtime = _runtime(ctx)
    gateway = _gateway(ctx)
    register_agent_route(gateway, lambda: runtime.load(force=True))
    records = asyncio.run(gateway.drain())
    runtime.emit_json({"records": [record.model_dump(mode="json") for record in records]})


@app.command("worker")
def worker(
    ctx: typer.Context,
    poll: float = typer.Option(1, "--poll", min=0.1, max=300),
) -> None:
    """Run the foreground gateway worker under a service supervisor."""

    runtime = _runtime(ctx)
    gateway = _gateway(ctx)
    register_agent_route(gateway, lambda: runtime.load(force=True))
    try:
        asyncio.run(gateway.serve(poll_seconds=poll))
    except KeyboardInterrupt:
        return
