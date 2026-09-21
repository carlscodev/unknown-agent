"""Browser tools: real Chromium validation through Playwright.

These tools give the agent *rendered* evidence - what the user actually sees and
what the console actually reports - instead of inferring behaviour from source.
"""

from __future__ import annotations

from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

FLOW_DOC = (
    "flow: a list of steps, each one of:\n"
    '  - {"action": "goto", "url": "/pricing"}\n'
    '  - {"action": "click", "selector": "text=Sign in", "or": "css:#login"}\n'
    '  - {"action": "fill", "selector": "input[name=email]", "value": "a@b.com"}\n'
    '  - {"action": "press", "selector": "body", "value": "Enter"}\n'
    '  - {"action": "wait_for", "selector": "#dashboard", "timeout_ms": 10000}\n'
    '  - {"action": "assert_text", "text": "Welcome back"}\n'
    '  - {"action": "assert_visible", "selector": "[data-testid=cart]"}'
)


class BrowserFlowTool(Tool):
    name = "browser_flow"
    description = (
        "Drive a real Chromium browser through a scripted flow (navigate, click, fill, assert) and "
        "return console errors, failed requests, screenshots and a per-step pass/fail report. "
        "Use it to verify front-end work instead of assuming it renders.\n\n" + FLOW_DOC
    )
    category = "browser"
    risk = RiskLevel.NETWORK
    requires_extra = "browser"
    tags = ("verify", "browser", "primary")
    timeout_s = 300.0
    parameters = (
        ParameterSpec("url", "string", "Start URL (absolute, or a path resolved against the app base URL).", default=""),
        ParameterSpec("flow", "array", "Steps to execute.", default=None, items="object"),
        ParameterSpec("screenshot", "string", "Save a full-page screenshot to this path.", default=""),
        ParameterSpec("headless", "boolean", "Run headless.", default=True),
        ParameterSpec("start_app", "boolean", "Start the configured dev server first if the URL is not reachable.", default=False),
    )
    examples = ('ua tools call browser_flow --arg url=http://localhost:3000 --arg flow=\'[{"action":"assert_text","text":"Dashboard"}]\'',)

    async def run(self, ctx: ToolContext, url: str = "", flow: list[dict[str, Any]] | None = None, screenshot: str = "", headless: bool = True, start_app: bool = False) -> ToolResult:
        from unknown_agent.browser.manager import BrowserSessionManager

        # Fall back to the configured base URL, then to the project's detected dev URL,
        # so "validate the UI" works without the model having to guess a port.
        url = url or ctx.settings.browser.base_url or (getattr(ctx.project, "dev_url", "") or "")
        if not url:
            return ToolResult.failure(
                "No URL to validate. Pass url=..., set browser.base_url in config, or run `ua init --url ...`.",
                metadata={"hint": "The project's dev server URL was not detected."},
            )
        manager = ctx.browser or BrowserSessionManager(ctx.settings.browser, artifacts_dir=ctx.artifact_path(".").parent)
        owns = ctx.browser is None
        try:
            session = await manager.session(headless=headless, start_app=start_app, base_url=url or ctx.settings.browser.base_url)
            report = await session.run_flow(url=url or session.base_url or "", steps=flow or [])
            artifact = ""
            if screenshot:
                path = ctx.artifact_path(screenshot) if not str(screenshot).startswith("/") else screenshot
                await session.screenshot(str(path), full_page=True)
                artifact = str(path)
            payload = report.render()
            return ToolResult(
                ok=report.passed,
                output=payload,
                error=None if report.passed else f"{len(report.failures)} browser step(s) failed",
                artifacts=[artifact] if artifact else [],
                data={"passed": report.passed, "console_errors": report.console_errors, "failed_requests": report.failed_requests, "screenshots": report.artifacts},
            )
        finally:
            if owns:
                await manager.close()


class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Open a URL in Chromium and capture a screenshot plus console errors. Quick visual check."
    category = "browser"
    risk = RiskLevel.NETWORK
    requires_extra = "browser"
    tags = ("verify", "browser")
    timeout_s = 120.0
    parameters = (
        ParameterSpec("url", "string", "Page URL.", required=True),
        ParameterSpec("width", "integer", "Viewport width.", default=1440, minimum=320),
        ParameterSpec("height", "integer", "Viewport height.", default=900, minimum=240),
        ParameterSpec("full_page", "boolean", "Capture the full page.", default=True),
    )

    async def run(self, ctx: ToolContext, url: str, width: int = 1440, height: int = 900, full_page: bool = True) -> ToolResult:
        from unknown_agent.browser.manager import BrowserSessionManager

        # Fall back to the configured base URL, then to the project's detected dev URL,
        # so "validate the UI" works without the model having to guess a port.
        url = url or ctx.settings.browser.base_url or (getattr(ctx.project, "dev_url", "") or "")
        if not url:
            return ToolResult.failure(
                "No URL to validate. Pass url=..., set browser.base_url in config, or run `ua init --url ...`.",
                metadata={"hint": "The project's dev server URL was not detected."},
            )
        manager = ctx.browser or BrowserSessionManager(ctx.settings.browser, artifacts_dir=ctx.artifact_path(".").parent)
        owns = ctx.browser is None
        try:
            session = await manager.session(viewport=(width, height))
            report = await session.run_flow(url=url, steps=[{"action": "goto", "url": url}])
            path = ctx.artifact_path(f"screenshot-{abs(hash(url)) % 10_000}.png")
            await session.screenshot(str(path), full_page=full_page)
            return ToolResult(
                ok=report.passed,
                output=f"screenshot saved: {path}\nconsole errors: {len(report.console_errors)}\nfailed requests: {len(report.failed_requests)}",
                artifacts=[str(path)],
                data={"path": str(path), "console_errors": report.console_errors},
            )
        finally:
            if owns:
                await manager.close()


class BrowserConsoleTool(Tool):
    name = "browser_console"
    description = "Load a URL and return only the console messages and failed network requests, for debugging."
    category = "browser"
    risk = RiskLevel.NETWORK
    requires_extra = "browser"
    tags = ("debug", "browser")
    timeout_s = 120.0
    parameters = (
        ParameterSpec("url", "string", "Page URL.", required=True),
        ParameterSpec("wait_ms", "integer", "Extra time to let the page settle.", default=2000, minimum=0, maximum=30_000),
    )

    async def run(self, ctx: ToolContext, url: str, wait_ms: int = 2000) -> ToolResult:
        from unknown_agent.browser.manager import BrowserSessionManager

        # Fall back to the configured base URL, then to the project's detected dev URL,
        # so "validate the UI" works without the model having to guess a port.
        url = url or ctx.settings.browser.base_url or (getattr(ctx.project, "dev_url", "") or "")
        if not url:
            return ToolResult.failure(
                "No URL to validate. Pass url=..., set browser.base_url in config, or run `ua init --url ...`.",
                metadata={"hint": "The project's dev server URL was not detected."},
            )
        manager = ctx.browser or BrowserSessionManager(ctx.settings.browser, artifacts_dir=ctx.artifact_path(".").parent)
        owns = ctx.browser is None
        try:
            session = await manager.session()
            report = await session.run_flow(url=url, steps=[{"action": "goto", "url": url}, {"action": "wait_for_timeout", "value": wait_ms}])
            lines = ["console:"]
            lines.extend(f"  {item}" for item in (report.console_errors or ["  (none)"]))
            lines.append("failed requests:")
            lines.extend(f"  {item}" for item in (report.failed_requests or ["  (none)"]))
            return ToolResult(ok=not report.console_errors and not report.failed_requests, output="\n".join(lines), data={"console_errors": report.console_errors, "failed_requests": report.failed_requests})
        finally:
            if owns:
                await manager.close()


def build_tools() -> list[Tool]:
    return [BrowserFlowTool(), BrowserScreenshotTool(), BrowserConsoleTool()]


__all__ = ["BrowserFlowTool", "BrowserScreenshotTool", "BrowserConsoleTool", "build_tools"]
