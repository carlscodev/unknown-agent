"""``ua browser`` - Chromium validation from the command line.

Humans get a real browser session for debugging; the agent gets the same
subsystem's structured output for gate evidence.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from unknown_agent.browser.flows import SUPPORTED_ACTIONS, discover_flows, load_flow, smoke_steps, write_flow
from unknown_agent.browser.manager import BrowserSessionManager, browsers_installed, playwright_available
from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import ConfigError, ExitCode, UsageError
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, elapsed

app = typer.Typer(name="browser", help="Drive Chromium: flows, screenshots, console capture.", no_args_is_help=True)


def _manager(ctx: typer.Context, *, headless: bool | None = None, url: str | None = None) -> BrowserSessionManager:
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    config = loaded.settings.browser.model_copy(deep=True)
    if headless is not None:
        config.headless = headless
    if url:
        config.base_url = url
    if not playwright_available()[0]:
        raise ConfigError(
            "Playwright is not installed.",
            hint="pipx inject unknown-agent playwright  (or: pip install 'unknown-agent[browser]')",
        )
    return BrowserSessionManager(config, artifacts_dir=loaded.paths.artifacts_dir)


@app.command("open")
def open_page(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="URL or path (resolved against the configured base URL)."),
    headless: bool = typer.Option(False, "--headless/--headed", help="Run headless (default: headed, so you can watch)."),
    wait: int = typer.Option(3, "--wait", help="Seconds to keep the page open."),
    screenshot: Path | None = typer.Option(None, "--screenshot", help="Save a screenshot before closing."),
    console: bool = typer.Option(True, "--console/--no-console", help="Print console errors and failed requests."),
) -> None:
    """Open a page in Chromium, report console/network errors, optionally keep it open.

    Examples:
      ua browser open http://localhost:3000 --headed
      ua browser open /login --screenshot login.png
    """
    runtime: Runtime = ctx.obj
    manager = _manager(ctx, headless=headless, url=url if url.startswith("http") else None)

    async def _run() -> dict:
        session = await manager.session(headless=headless, base_url=url if url.startswith("http") else None)
        report = await session.run_flow(url=url, steps=[{"action": "wait_for_timeout", "value": wait * 1000}])
        payload = report.to_dict()
        if screenshot:
            await session.screenshot(str(screenshot), full_page=True)
            payload["screenshot"] = str(screenshot)
        return payload

    try:
        payload = asyncio.run(_run())
    finally:
        asyncio.run(manager.close())

    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(f"[ua.ok]loaded[/ua.ok] {payload['url']} in {elapsed(payload['duration_ms'] / 1000)}")
    if payload.get("title"):
        output.print(f"title: {payload['title']}")
    if console and payload["console_errors"]:
        output.print("[ua.warn]console errors[/ua.warn]")
        for item in payload["console_errors"][:12]:
            output.print(f"  {item}")
    if console and payload["failed_requests"]:
        output.print("[ua.warn]failed requests[/ua.warn]")
        for item in payload["failed_requests"][:12]:
            output.print(f"  {item}")
    if payload.get("screenshot"):
        output.print(f"screenshot: {payload['screenshot']}")


@app.command("flow")
def flow(
    ctx: typer.Context,
    url: str | None = typer.Option(None, "--url", help="Start URL."),
    steps_file: Path | None = typer.Option(None, "--steps-file", help="YAML/JSON flow file."),
    flow_name: str | None = typer.Option(None, "--flow", help="Named flow discovered in the project."),
    screenshot: Path | None = typer.Option(None, "--screenshot", help="Save a screenshot when the flow ends."),
    headless: bool = typer.Option(True, "--headless/--headed"),
    start_app: bool = typer.Option(False, "--start-app", help="Start the configured dev server first."),
    timeout: int | None = typer.Option(None, "--timeout", help="Per-step timeout in milliseconds."),
) -> None:
    """Execute a browser flow and report per-step pass/fail with console evidence.

    Examples:
      ua browser flow --url http://localhost:3000 --steps-file .unknown/flows/checkout.yaml
      ua browser flow --flow signup --screenshot after.png
      ua browser flow --url http://localhost:3000 --start-app
    """
    runtime: Runtime = ctx.obj
    loaded = runtime.load()

    steps: list[dict] = []
    if flow_name:
        matches = [item for item in discover_flows(loaded.project_root or loaded.paths.cwd) if item.name == flow_name]
        if not matches:
            raise UsageError(f"No flow named {flow_name!r} found.", hint="Run `ua browser flows` to list discovered flows.")
        flow_obj = matches[0]
        steps = flow_obj.steps
        url = url or flow_obj.url
    elif steps_file:
        flow_obj = load_flow(steps_file)
        steps = flow_obj.steps
        url = url or flow_obj.url
    else:
        steps = smoke_steps(None)

    manager = _manager(ctx, headless=headless, url=url)
    if timeout:
        manager.config.timeout_ms = timeout

    async def _run() -> dict:
        try:
            session = await manager.session(headless=headless, base_url=url, start_app=start_app)
            report = await session.run_flow(url=url or "", steps=steps)
            payload = report.to_dict()
            if screenshot:
                await session.screenshot(str(screenshot), full_page=True)
                payload["screenshot"] = str(screenshot)
            return payload
        finally:
            await manager.close()

    payload = asyncio.run(_run())
    if runtime.is_machine:
        runtime.emit_json(payload)
        if not payload["passed"]:
            raise typer.Exit(ExitCode.GATE)
        return

    output.blank()
    output.print(f"[{'ua.ok' if payload['passed'] else 'ua.err'}]{'PASS' if payload['passed'] else 'FAIL'}[/{'ua.ok' if payload['passed'] else 'ua.err'}] "
                 f"{payload['url']} ({len(payload['steps'])} steps, {elapsed(payload['duration_ms'] / 1000)})")
    rows = [(step["index"], step["action"], "ok" if step["ok"] else "FAIL", f"{step['duration_ms']:.0f}ms", step["detail"][:70]) for step in payload["steps"]]
    output.print(data_table(["#", "action", "result", "time", "detail"], rows))
    for label, key in (("console errors", "console_errors"), ("failed requests", "failed_requests"), ("page errors", "page_errors")):
        if payload[key]:
            output.print(f"[ua.warn]{label}[/ua.warn]")
            for item in payload[key][:10]:
                output.print(f"  {item}")
    if payload.get("screenshot"):
        output.print(f"screenshot: {payload['screenshot']}")
    if not payload["passed"]:
        raise typer.Exit(ExitCode.GATE)


@app.command("flows")
def flows(ctx: typer.Context, json_output: bool = typer.Option(False, "--json", help="Machine-readable list.")) -> None:
    """List flow files discovered in this project."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    items = discover_flows(loaded.project_root or loaded.paths.cwd)
    if runtime.is_machine or json_output:
        runtime.emit_json({"flows": [item.to_dict() for item in items], "actions": list(SUPPORTED_ACTIONS)})
        return
    if not items:
        output.print("[ua.muted]no flows found in .unknown/flows[/ua.muted]")
        output.print("[ua.muted]create one with: ua browser flow --url ... (then 'ua browser save-flow')[/ua.muted]")
        return
    output.print(data_table(["name", "steps", "url", "file"], [(item.name, len(item.steps), item.url or "-", str(item.path)) for item in items], title="flows"))


@app.command("save-flow")
def save_flow(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Flow name."),
    steps_json: str = typer.Option(..., "--steps", help="JSON array of steps."),
    url: str | None = typer.Option(None, "--url"),
) -> None:
    """Persist a flow so CI and future runs can replay it."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    try:
        steps = json.loads(steps_json)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--steps must be a JSON array: {exc}") from exc
    if not isinstance(steps, list):
        raise UsageError("--steps must be a JSON array of step objects.")
    path = write_flow(loaded.project_root or loaded.paths.cwd, name, steps, url=url)
    output.print(f"[ua.ok]saved[/ua.ok] flow {name!r} to {path}")


@app.command("screenshot")
def screenshot(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Page URL."),
    out: Path = typer.Option(Path("screenshot.png"), "--out", help="Output file."),
    width: int = typer.Option(1440, "--width"),
    height: int = typer.Option(900, "--height"),
    full_page: bool = typer.Option(True, "--full/--viewport", help="Capture the full page or just the viewport."),
) -> None:
    """Capture a screenshot and report console errors."""

    runtime: Runtime = ctx.obj
    manager = _manager(ctx, url=url)

    async def _run() -> dict:
        try:
            session = await manager.session(viewport=(width, height), base_url=url)
            report = await session.run_flow(url=url, steps=[])
            path = await session.screenshot(str(out), full_page=full_page)
            return {"path": path, "console_errors": report.console_errors, "failed_requests": report.failed_requests}
        finally:
            await manager.close()

    payload = asyncio.run(_run())
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    output.print(f"[ua.ok]saved[/ua.ok] {payload['path']}")
    if payload["console_errors"]:
        output.print(f"[ua.warn]{len(payload['console_errors'])} console error(s)[/ua.warn]")


@app.command("install")
def install(
    ctx: typer.Context,
    with_deps: bool = typer.Option(False, "--with-deps", help="Install OS-level Chromium dependencies (root only)."),
) -> None:
    """Install Playwright browser binaries (alias of `ua doctor --install-browsers`)."""

    runtime: Runtime = ctx.obj
    from unknown_agent.browser.manager import install_browsers

    with output.status("installing Chromium..."):
        ok, log = asyncio.run(install_browsers("chromium", with_deps=with_deps))
    if ok:
        output.print("[ua.ok]Chromium installed[/ua.ok]")
        return
    output.error_panel("browser install failed", log[-1500:], hint="Try --with-deps, or install system chromium")
    raise typer.Exit(ExitCode.FAILURE)


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Report browser subsystem readiness."""

    runtime: Runtime = ctx.obj
    available, detail = playwright_available()
    installed, installed_detail = browsers_installed() if available else (False, "playwright missing")
    payload = {"playwright": available, "detail": detail, "browsers": installed, "browsers_detail": installed_detail}
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    from unknown_agent.ui.renderers import kv_table

    output.print(kv_table([("playwright", "installed" if available else detail), ("chromium", "installed" if installed else installed_detail)]))


__all__ = ["app"]
