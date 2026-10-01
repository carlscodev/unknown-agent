"""``ua gateway`` — operate the durable channel-neutral inbox and outbox."""

from __future__ import annotations

import asyncio

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.gateway import (
    AccessController,
    ChannelAddress,
    ContentKind,
    ContentPart,
    DeviceController,
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


def _access(ctx: typer.Context) -> AccessController:
    loaded = _runtime(ctx).load()
    return AccessController(loaded.paths.project_dir / "gateway.db")


def _gateway(ctx: typer.Context) -> Gateway:
    loaded = _runtime(ctx).load()
    return Gateway(loaded.paths.project_dir / "gateway.db", access=_access(ctx))


def _devices(ctx: typer.Context) -> DeviceController:
    loaded = _runtime(ctx).load()
    return DeviceController(loaded.paths.project_dir / "gateway.db")


@app.command("device-enroll-issue")
def device_enroll_issue(
    ctx: typer.Context,
    ttl: int = typer.Option(300, "--ttl", min=1, max=300),
) -> None:
    """Issue a one-time cryptographic device enrollment token."""
    enrollment = _devices(ctx).issue_enrollment(ttl_seconds=ttl)
    _runtime(ctx).emit_json(enrollment.model_dump(mode="json"))


@app.command("pair-issue")
def pair_issue(
    ctx: typer.Context,
    channel: str = typer.Option(..., "--channel"),
    account: str = typer.Option(..., "--account"),
    role: str = typer.Option("operator", "--role"),
    ttl: int = typer.Option(300, "--ttl", min=1, max=300),
) -> None:
    """Issue a one-time six-digit pairing code."""
    pairing = _access(ctx).issue(channel, account, role=role, ttl_seconds=ttl)  # type: ignore[arg-type]
    _runtime(ctx).emit_json(pairing.model_dump(mode="json"))


@app.command("pair-claim")
def pair_claim(
    ctx: typer.Context,
    channel: str = typer.Option(..., "--channel"),
    account: str = typer.Option(..., "--account"),
    sender: str = typer.Option(..., "--sender"),
    code: str = typer.Option(..., "--code"),
) -> None:
    """Bind a channel sender to a one-time pairing code."""
    identity = _access(ctx).claim(channel, account, sender, code)
    _runtime(ctx).emit_json(identity.model_dump(mode="json"))


@app.command("identities")
def identities(ctx: typer.Context) -> None:
    """List paired channel identities."""
    values = _access(ctx).list()
    _runtime(ctx).emit_json({"identities": [item.model_dump(mode="json") for item in values]})


@app.command("revoke")
def revoke(ctx: typer.Context, identity_id: str) -> None:
    """Disable a paired identity immediately."""
    _runtime(ctx).emit_json({"revoked": _access(ctx).revoke(identity_id), "id": identity_id})


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
