"""Agent-facing computer-use tools: ``computer_observe`` and ``computer_act``.

The pair implements the observe/decide/act loop from the model's side:

* ``computer_observe`` returns the current screenshot path + a text snapshot,
  so a vision-capable model can look and a text model can still reason.
* ``computer_act`` takes ONE action from the closed vocabulary and returns the
  resulting observation.  One action per call keeps the confirmation policy
  meaningful and the journal exact.

Session lifetime: the executor stores the live session in ``ctx.extra`` under
``computer_use_session`` so consecutive calls share budgets and the journal.
"""

from __future__ import annotations

from typing import Any

from unknown_agent.computer_use.actions import parse_action
from unknown_agent.computer_use.session import ComputerUseError, ComputerUseSession
from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

__all__ = ["ComputerObserveTool", "ComputerActTool", "build_tools"]

_SESSION_KEY = "computer_use_session"


async def _session(ctx: ToolContext) -> ComputerUseSession:
    existing = (ctx.extra or {}).get(_SESSION_KEY)
    if existing is not None:
        return existing
    if ctx.browser is None:
        raise ComputerUseError(
            "Computer use needs the managed browser.",
            hint="Install Playwright (`ua doctor --install-browsers`) and enable [browser].",
        )
    from unknown_agent.computer_use.driver import BrowserDriver

    config = ctx.settings.computer_use
    driver = BrowserDriver(
        ctx.browser,
        viewport=(ctx.settings.browser.viewport_width, ctx.settings.browser.viewport_height),
    )
    session = ComputerUseSession(
        config,
        driver,
        journal_dir=ctx.paths.project_dir / "computer-use",
        artifacts_dir=ctx.artifacts_dir or ctx.paths.artifacts_dir,
        prompter=ctx.prompter,
    )
    ctx.extra[_SESSION_KEY] = session
    return session


class ComputerObserveTool(Tool):
    name = "computer_observe"
    description = (
        "Observe the current UI state: screenshot + visible-text snapshot + URL. "
        "Use before deciding on an action, and to verify an action's outcome."
    )
    category = "computer_use"
    risk = RiskLevel.READ
    tags = ("computer-use", "browser")
    requires_extra = "browser"
    concurrency_safe = False
    timeout_s = 60.0
    parameters = ()

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            session = await _session(ctx)
            observation = await session.observe()
        except ComputerUseError as exc:
            return ToolResult.failure(f"{exc.message} Hint: {exc.hint}")
        summary = f"url: {observation.url}\ntitle: {observation.title}\n"
        if observation.screenshot_path:
            summary += f"screenshot: {observation.screenshot_path}\n"
        if observation.dom_text:
            summary += f"visible text (truncated):\n{observation.dom_text[:2500]}"
        result = ToolResult.success(summary, **observation.to_dict())
        if observation.screenshot_path:
            result.artifacts = [observation.screenshot_path]
        return result


class ComputerActTool(Tool):
    name = "computer_act"
    description = (
        "Perform ONE UI action and observe the result. Actions: navigate(text=url), "
        "click(x,y), double_click(x,y), type(text), key(text), scroll(dx,dy), hover(x,y), "
        "wait(ms), back. Always give 'intent' - it is what the user approves."
    )
    category = "computer_use"
    risk = RiskLevel.EXECUTE
    tags = ("computer-use", "browser")
    requires_extra = "browser"
    concurrency_safe = False
    timeout_s = 90.0
    parameters = (
        ParameterSpec("kind", description="Action kind (navigate, click, type, key, scroll, hover, wait, back).", required=True),
        ParameterSpec("x", type="integer", description="Pointer x (click/hover)."),
        ParameterSpec("y", type="integer", description="Pointer y (click/hover)."),
        ParameterSpec("text", description="URL for navigate, text for type, key chord for key."),
        ParameterSpec("dx", type="integer", description="Horizontal scroll delta."),
        ParameterSpec("dy", type="integer", description="Vertical scroll delta."),
        ParameterSpec("ms", type="integer", description="Wait duration in milliseconds."),
        ParameterSpec("intent", description="Human-readable purpose of the action (shown in confirmations)."),
    )
    examples = (
        'computer_act(kind="navigate", text="/login", intent="open the login page")',
        'computer_act(kind="click", x=640, y=380, intent="click the Submit button")',
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            action = parse_action({key: value for key, value in kwargs.items() if value is not None})
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        try:
            session = await _session(ctx)
        except ComputerUseError as exc:
            return ToolResult.failure(f"{exc.message} Hint: {exc.hint}")
        result = await session.perform(action)
        observation = result.observation
        lines = [f"action: {action.describe()}", f"ok: {result.ok}"]
        if result.denied:
            lines.append(f"DENIED: {result.error}")
        elif result.error:
            lines.append(f"error: {result.error}")
        if observation is not None:
            lines.append(f"now at: {observation.url} ({observation.title})")
            if observation.screenshot_path:
                lines.append(f"screenshot: {observation.screenshot_path}")
            if observation.dom_text:
                lines.append(f"visible text (truncated):\n{observation.dom_text[:2000]}")
        lines.append(f"actions remaining: {session.remaining_actions}")
        payload = ToolResult(
            ok=result.ok,
            output="\n".join(lines),
            error=None if result.ok else (result.error or "action failed"),
            data=result.to_dict(),
        )
        if observation is not None and observation.screenshot_path:
            payload.artifacts = [observation.screenshot_path]
        return payload


def build_tools() -> list[Tool]:
    return [ComputerObserveTool(), ComputerActTool()]
