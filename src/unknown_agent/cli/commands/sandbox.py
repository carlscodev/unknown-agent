"""``ua sandbox`` — inspect native subprocess containment."""

from __future__ import annotations

import json

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.sandbox import SandboxRequest, build_sandbox_command, sandbox_status

app = typer.Typer(name="sandbox", no_args_is_help=True)


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if not isinstance(runtime, Runtime):
        raise UsageError("CLI runtime is not initialised.")
    return runtime


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Report backend availability and the effective configured profile."""

    runtime = _runtime(ctx)
    loaded = runtime.load()
    backend = sandbox_status()
    config = loaded.settings.sandbox
    runtime.emit_json(
        {
            **backend.model_dump(mode="json"),
            "mode": config.mode,
            "default_profile": config.default_profile,
            "shell_profile": config.shell_profile,
            "validation_profile": config.validation_profile,
            "network_allowed": config.allow_network,
        }
    )


@app.command("explain")
def explain(
    ctx: typer.Context,
    command: str = typer.Argument(..., help="Command used only to preview containment."),
) -> None:
    """Show the exact sandbox decision without starting the command."""

    runtime = _runtime(ctx)
    loaded = runtime.load()
    config = loaded.settings.sandbox
    request = SandboxRequest(
        mode=config.mode,
        profile=config.default_profile,
        workspace=loaded.paths.cwd,
        cwd=loaded.paths.cwd,
        allow_network=config.allow_network and loaded.settings.policy.allow_network,
    )
    decision = build_sandbox_command([command], request)
    # Keep this surface machine-safe; argv contains no secret values.
    runtime.emit_json(json.loads(decision.model_dump_json()))
