"""``ua marketplace`` — verified extension discovery and installation."""

from __future__ import annotations

import asyncio

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.extensions.manifest import TrustStore
from unknown_agent.marketplace import InstalledPackage, Marketplace

app = typer.Typer(name="marketplace", no_args_is_help=True)


def _runtime(ctx: typer.Context) -> Runtime:
    if not isinstance(ctx.obj, Runtime):
        raise UsageError("CLI runtime is not initialised.")
    return ctx.obj


def _market(ctx: typer.Context, registry_key: str) -> Marketplace:
    loaded = _runtime(ctx).load()
    return Marketplace(
        loaded.paths.user_plugins_dir,
        loaded.paths.state_dir / "marketplace.json",
        TrustStore(loaded.paths.config_dir / "trusted_publishers.json"),
        registry_key,
    )


@app.command("install")
def install(
    ctx: typer.Context,
    name: str,
    index_url: str = typer.Option(..., "--index"),
    registry_key: str = typer.Option(..., "--registry-key"),
    version: str | None = typer.Option(None, "--version"),
    allow_downgrade: bool = typer.Option(False, "--allow-downgrade"),
) -> None:
    """Install a package after index, archive, publisher, and manifest verification."""

    async def operation() -> InstalledPackage:
        market = _market(ctx, registry_key)
        index = await market.fetch_index(index_url)
        return await market.install(index, name, version=version, allow_downgrade=allow_downgrade)

    result = asyncio.run(operation())
    _runtime(ctx).emit_json(result.model_dump(mode="json"))


@app.command("list")
def list_installed(
    ctx: typer.Context,
    registry_key: str = typer.Option(..., "--registry-key"),
) -> None:
    """List marketplace-managed packages."""
    values = _market(ctx, registry_key).installed()
    _runtime(ctx).emit_json(
        {"packages": [item.model_dump(mode="json") for item in values.values()]}
    )


@app.command("uninstall")
def uninstall(
    ctx: typer.Context,
    name: str,
    registry_key: str = typer.Option(..., "--registry-key"),
) -> None:
    """Remove a marketplace-managed package and signed manifest."""
    _runtime(ctx).emit_json({"name": name, "removed": _market(ctx, registry_key).uninstall(name)})
