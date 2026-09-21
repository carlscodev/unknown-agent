"""``ua vision`` — see what the agent can see.

Three verbs:

* ``status``   which model would answer visual questions right now (active or
               fallback), the ingestion policy, and whether Pillow is present
* ``analyze``  run an image + question through the real vision pipeline -
               the exact code path the agent's tools use, so what works here
               works in a run
* ``capabilities``  the full per-profile capability matrix (tools, vision,
               reasoning, streaming, computer use) with the *source* of each
               verdict - profile override, live metadata, catalog or heuristic

Everything honours ``--json``.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UnknownAgentError, UsageError
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table, summary_panel

app = typer.Typer(
    name="vision",
    help="Vision: capability matrix, routing status, direct image analysis.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

JSON_FLAG = typer.Option(False, "--json", help="Machine-readable JSON on stdout.")

F = TypeVar("F", bound=Callable[..., None])


def _guarded(func: F) -> F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            func(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except UnknownAgentError as exc:
            from unknown_agent.ui.errors import render_error

            raise typer.Exit(render_error(exc)) from None

    return wrapper  # type: ignore[return-value]


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if runtime is None:  # pragma: no cover
        raise UsageError("No runtime on the Typer context.")
    return runtime


def _machine(runtime: Runtime, json_flag: bool) -> bool:
    if json_flag and not runtime.is_machine:
        output.mode = "json"
    return runtime.is_machine or json_flag


def _capability_row(name: str, config: Any) -> dict[str, Any]:
    from unknown_agent.providers.capabilities import resolve_capabilities

    caps = resolve_capabilities(config, provider_name=name)
    return {"profile": name, "model": config.model, **caps.to_dict()}


# --------------------------------------------------------------------------- #
@app.command("status")
@_guarded
def status(ctx: typer.Context, json_flag: bool = JSON_FLAG) -> None:
    """Which model answers visual questions right now, and under what policy."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    settings = runtime.settings

    from unknown_agent.vision.images import pillow_available
    from unknown_agent.vision.service import VisionService, VisionUnavailable

    service = VisionService(settings)
    route_error = ""
    profile, is_fallback = "", False
    try:
        profile, is_fallback = service.route()
    except VisionUnavailable as exc:
        route_error = f"{exc.message} {exc.hint or ''}".strip()

    vision = settings.vision
    payload = {
        "enabled": vision.enabled,
        "route": {
            "profile": profile,
            "fallback": is_fallback,
            "error": route_error,
        },
        "policy": {
            "allowed_sources": list(vision.allowed_sources),
            "max_image_bytes": vision.max_image_bytes,
            "max_images_per_request": vision.max_images_per_request,
            "downscale_to_px": vision.downscale_to_px,
            "keep_copies_in_artifacts": vision.keep_copies_in_artifacts,
            "allow_remote_urls_in_tools": vision.allow_remote_urls_in_tools,
            "fallback_profile": vision.fallback_profile,
        },
        "pillow": pillow_available(),
    }
    if machine:
        runtime.emit_json(payload)
        return

    if not vision.enabled:
        route_line = "disabled ([vision] enabled = false)"
    elif route_error:
        route_line = f"[ua.err]unavailable[/] — {route_error}"
    elif is_fallback:
        route_line = f"fallback profile [bold]{profile}[/] (active model has no vision)"
    else:
        route_line = f"active profile [bold]{profile}[/]"
    rows = [
        ("Routing", route_line),
        ("Sources", ", ".join(vision.allowed_sources) or "none"),
        ("Max image size", f"{vision.max_image_bytes // 1024} KiB"),
        ("Max images/request", str(vision.max_images_per_request)),
        ("Downscale to", f"{vision.downscale_to_px} px" + ("" if payload["pillow"] else "  (Pillow missing → no downscaling)")),
        ("Artifact copies", "yes" if vision.keep_copies_in_artifacts else "no"),
        ("Remote URLs from tools", "allowed" if vision.allow_remote_urls_in_tools else "blocked (exfiltration guard)"),
    ]
    output.print(summary_panel("Vision", rows))
    if not payload["pillow"]:
        output.print("[ua.dim]Install the vision extra for downscaling: pip install 'unknown-agent[vision]'[/]")


# --------------------------------------------------------------------------- #
@app.command("capabilities")
@_guarded
def capabilities(ctx: typer.Context, json_flag: bool = JSON_FLAG) -> None:
    """Per-profile capability matrix with the source of every verdict."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    settings = runtime.settings

    rows = [_capability_row(name, config) for name, config in sorted(settings.providers.items())]
    if machine:
        runtime.emit_json({"profiles": rows})
        return

    def cell(entry: dict[str, Any]) -> str:
        mark = "[ua.ok]yes[/]" if entry["value"] else "[ua.dim]no[/]"
        return f"{mark} [ua.dim]({entry['source']})[/]"

    table_rows = [
        (
            row["profile"],
            row["model"],
            cell(row["tools"]),
            cell(row["vision"]),
            cell(row["reasoning"]),
            cell(row["streaming"]),
            cell(row["computer_use"]),
        )
        for row in rows
    ]
    output.print(
        data_table(
            ("Profile", "Model", "Tools", "Vision", "Reasoning", "Streaming", "Computer use"),
            table_rows,
            title="Model capabilities",
        )
    )
    output.print(
        "[ua.dim]sources: profile = explicit override · metadata = live model listing · "
        "catalog = provider catalog · heuristic = model-id pattern · default = assumed[/]"
    )


# --------------------------------------------------------------------------- #
@app.command("analyze")
@_guarded
def analyze(
    ctx: typer.Context,
    image: list[str] = typer.Argument(..., help="Image path(s), data URL(s) or https URL(s)."),
    question: str = typer.Option("Describe the image precisely.", "--question", "-q", help="What to determine."),
    json_flag: bool = JSON_FLAG,
) -> None:
    """Analyze image(s) through the exact pipeline the agent's tools use."""

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_flag)
    settings = runtime.settings

    from unknown_agent.vision.images import load_image
    from unknown_agent.vision.service import VisionService

    attachments = [
        load_image(source, settings.vision, project_root=runtime.paths.cwd)
        for source in image
    ]

    async def _run() -> Any:
        service = VisionService(settings)
        try:
            return await service.analyze(attachments, question)
        finally:
            await service.aclose()

    answer = asyncio.run(_run())
    if machine:
        runtime.emit_json(answer.to_dict())
        return
    header = f"[bold]{answer.model}[/]" + ("  [ua.dim](fallback profile)[/]" if answer.routed else "")
    output.print(header)
    output.print(answer.text)


__all__ = ["app"]
