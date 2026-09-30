"""``ua acp`` — ACP v1 JSON-RPC agent over stdio."""

from __future__ import annotations

import asyncio

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.rpc import AcpAgent


def acp_command(
    ctx: typer.Context,
    timeout: float = typer.Option(
        300, "--timeout", min=1, max=300, help="Maximum seconds for one RPC request."
    ),
) -> None:
    """Serve ACP v1 as newline-delimited JSON-RPC 2.0 over stdin/stdout."""

    runtime = ctx.obj
    if not isinstance(runtime, Runtime):
        raise UsageError("CLI runtime is not initialised.")
    agent = AcpAgent(lambda: runtime.load(force=True), timeout_seconds=timeout)
    asyncio.run(agent.rpc.serve_stdio())
