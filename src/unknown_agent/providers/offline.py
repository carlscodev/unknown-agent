"""Deterministic offline provider.

Purpose: the product must be usable and testable **without any API key**.  This
provider implements the same contract as a real model but answers from rules:

* in plan mode it emits a structured plan derived from the task and tool list;
* in tool mode it chooses a sensible first tool for the task and then closes the
  loop with a summary of the evidence it observed.

It is the default for ``--provider offline``, it powers the test suite (so tests
never touch the network and never depend on model behaviour), and it lets people
evaluate the CLI, the tool layer, the validation engine and the browser
subsystem before deciding which model to pay for.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from unknown_agent.core.models import Message, ToolCall
from unknown_agent.providers.base import ModelProvider, ModelRequest, ModelResponse, StreamChunk, Usage

#: task keyword -> preferred tool, first match wins.
TOOL_HINTS: list[tuple[str, str]] = [
    (r"\b(browser|ui|page|render|screenshot|click|form|frontend|visual)\b", "browser_flow"),
    (r"\b(test|pytest|jest|vitest|coverage|spec)\b", "test_run"),
    (r"\b(build|compile|bundle|package|bundler)\b", "build_run"),
    (r"\b(lint|eslint|ruff|flake8|style)\b", "lint_run"),
    (r"\b(typecheck|mypy|tsc|types)\b", "typecheck_run"),
    (r"\b(git|commit|branch|diff|status|history|blame)\b", "git_status"),
    (r"\b(install|dependency|dependencies|requirements|package\.json|lockfile)\b", "package_info"),
    (r"\b(run|execute|command|shell)\b", "shell_run"),
    (r"\b(search|find|grep|where|locate|usages?)\b", "search_code"),
    (r"\b(read|show|open|inspect|explain|what is|how does)\b", "read_file"),
    (r"\b(tree|structure|layout|layout|folders?)\b", "project_tree"),
]

PLAN_STEP_TEMPLATE = [
    {
        "title": "Reconnaissance",
        "description": "Map the repository, detect the stack and locate the code that owns the request.",
        "tools": ["project_tree", "search_code", "read_file"],
        "acceptance": ["Relevant files identified", "Project commands discovered"],
    },
    {
        "title": "Implementation",
        "description": "Apply the smallest coherent change that satisfies the request, following local conventions.",
        "tools": ["apply_patch", "write_file"],
        "acceptance": ["Change is scoped", "No unrelated files touched"],
    },
    {
        "title": "Verification",
        "description": "Run the project's own build, tests, lint and browser checks until they pass.",
        "tools": ["test_run", "build_run", "lint_run", "browser_flow"],
        "acceptance": ["Required gates pass", "Evidence captured"],
    },
]


class OfflineProvider(ModelProvider):
    """Rule-based provider with a real tool-calling protocol."""

    name = "offline"
    supports_tools = True
    supports_streaming = False
    supports_json_mode = True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        tool_names = {tool.name for tool in request.tools}
        task = _last_user_text(request.messages)
        tool_results = [message for message in request.messages if message.role == "tool"]

        if request.json_mode and _looks_like_planner(request.messages):
            payload = self._plan_payload(task, tool_names)
            return self._response(json.dumps(payload, ensure_ascii=False), request, finish="stop")

        if tool_names and not tool_results:
            choice = self._choose_tool(task, tool_names)
            if choice:
                call = ToolCall.new(choice["name"], choice["arguments"])
                return self._response("", request, finish="tool_calls", calls=[call])

        summary = self._summarise(task, tool_results)
        return self._response(summary, request, finish="stop")

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        response = await self.complete(request)
        if response.content:
            for token in re.findall(r"\S+\s*", response.content):
                yield StreamChunk(delta=token)
        for call in response.tool_calls:
            yield StreamChunk(tool_call=call)
        yield StreamChunk(finish_reason=response.finish_reason, usage=response.usage)

    # -- internals ---------------------------------------------------------
    def _response(
        self,
        content: str,
        request: ModelRequest,
        *,
        finish: str,
        calls: list[ToolCall] | None = None,
    ) -> ModelResponse:
        prompt_tokens = sum(self.estimate_tokens(message.content) for message in request.messages)
        completion_tokens = self.estimate_tokens(content) if content else 8
        return ModelResponse(
            content=content,
            tool_calls=calls or [],
            finish_reason=finish,  # type: ignore[arg-type]
            usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=prompt_tokens + completion_tokens, estimated=True),
            model=self.model,
            provider=self.name,
            latency_ms=1.0,
            raw={"deterministic": True},
        )

    def _plan_payload(self, task: str, tool_names: set[str]) -> dict[str, Any]:
        steps = []
        for template in PLAN_STEP_TEMPLATE:
            tools = [tool for tool in template["tools"] if tool in tool_names] or sorted(tool_names)[:3]
            steps.append({**template, "tools": tools})
        return {
            "goal": task or "Improve the repository",
            "deliverables": ["Scoped code change", "Passing verification gates", "Short evidence-backed summary"],
            "risks": ["Unrelated files edited", "Gates not executed", "Assumptions about the stack"],
            "verification": ["Project test command", "Project build command", "Lint and typecheck when configured"],
            "assumptions": ["Offline deterministic planner used", "Tool results reflect the real repository"],
            "steps": steps,
        }

    def _choose_tool(self, task: str, tool_names: set[str]) -> dict[str, Any] | None:
        lowered = (task or "").lower()
        for pattern, tool in TOOL_HINTS:
            if re.search(pattern, lowered):
                if tool in tool_names:
                    return {"name": tool, "arguments": _default_arguments(tool, task)}
        for fallback in ("inspect_project", "project_tree", "list_files", "git_status"):
            if fallback in tool_names:
                return {"name": fallback, "arguments": _default_arguments(fallback, task)}
        return None

    def _summarise(self, task: str, tool_results: list[Message]) -> str:
        if not tool_results:
            return (
                "No tool results are available yet. Offline mode provides deterministic planning and tool "
                "execution; configure a model provider (for example `[providers.default] api_key = "
                "\"env:OPENAI_API_KEY\"`) for full reasoning."
            )
        lines = ["Offline provider summary of observed evidence:"]
        for message in tool_results[-4:]:
            first_line = (message.content or "").strip().splitlines()
            preview = first_line[0][:160] if first_line else "(empty)"
            lines.append(f"- {message.name or 'tool'}: {preview}")
        lines.append("")
        lines.append(f"Task: {task or '(none)'}")
        lines.append("Next: configure a model provider for autonomous editing, or continue with explicit tool commands.")
        return "\n".join(lines)


def _default_arguments(tool: str, task: str) -> dict[str, Any]:
    sample = _first_path_like(task)
    mapping: dict[str, dict[str, Any]] = {
        "read_file": {"path": sample or "README.md"},
        "search_code": {"pattern": _first_word(task) or "TODO"},
        "list_files": {"path": "."},
        "project_tree": {"path": ".", "depth": 2},
        "git_status": {},
        "inspect_project": {},
        "test_run": {},
        "build_run": {},
        "lint_run": {},
        "typecheck_run": {},
        "browser_flow": {},
        "package_info": {},
        "shell_run": {"command": "git status --short"},
    }
    return mapping.get(tool, {})


def _last_user_text(messages: list[Message]) -> str:
    """Return the most relevant user text.

    Step prompts embed the whole plan (including phrases like "browser checks"),
    which would steer a keyword-based planner towards the wrong tool. The explicit
    ``Task:`` line is therefore preferred when present.
    """

    for message in reversed(messages):
        if message.role != "user":
            continue
        for line in message.content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("task:"):
                return stripped.split(":", 1)[1].strip() or stripped
        return message.content
    return ""


def _looks_like_planner(messages: list[Message]) -> bool:
    for message in messages:
        if message.role == "system":
            lowered = message.content.lower()
            if "plan" in lowered and ("json" in lowered or "step" in lowered):
                return True
    return False


def _first_word(text: str) -> str | None:
    match = re.search(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", text or "")
    return match.group(0) if match else None


def _first_path_like(text: str) -> str | None:
    match = re.search(r"[\w./-]+\.(py|ts|tsx|js|jsx|go|rs|rb|php|java|kt|md|json|toml|yaml|yml)", text or "")
    return match.group(0) if match else None


def build(**kwargs: Any) -> ModelProvider:
    return OfflineProvider(**kwargs)


__all__ = ["OfflineProvider", "build", "TOOL_HINTS"]
