"""Tool-enabled, streaming chat loop for the web panel.

Why this module exists
----------------------
The panel used to have two modes and both were half-useful:

* *chat* — the model was called with no tools at all, so when the user asked for
  a landing page the model invented excuses ("no tengo herramientas conectadas a
  tu sistema") and dumped code nobody could run or preview;
* *agent* — the full :class:`~unknown_agent.core.runner.Runner` ran, but it only
  emitted events at step granularity, so nothing streamed and the "thinking"
  window was a decoration above the composer.

:class:`WebAgent` is the missing middle: the same tool registry, policy engine
and executor the CLI uses (so writes are real and policy-checked), driven by a
streaming tool-calling loop that reports, live and separately:

* ``reasoning``  — the model's internal thinking, token by token;
* ``delta``      — the visible answer, token by token;
* ``tool``       — every tool call with its arguments, result and duration;
* ``artifact``   — every file written, with a preview kind (html/image/pdf/md);
* ``phase``      — thinking / generating / tools, with authoritative timings.

It is the OpenCode-style contract the panel needs: read, write, search and run
inside the workspace the server was started in, with the answer streaming while
the tools run.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from unknown_agent.config.models import Settings
from unknown_agent.config.paths import AppPaths
from unknown_agent.core.models import Message, ToolCall, ToolResult
from unknown_agent.observability.events import EventBus
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import ModelRequest, ToolSchema
from unknown_agent.tools.base import ToolContext
from unknown_agent.tools.executor import ToolExecutor
from unknown_agent.tools.factory import build_default_registry
from unknown_agent.tools.policy import PolicyEngine
from unknown_agent.tools.registry import ToolRegistry

log = get_logger(__name__)

#: Categories exposed to the browser.  Planning/MCP/skill-authoring tools are
#: withheld: they need a terminal to be useful and only inflate the payload.
WEB_TOOL_CATEGORIES = (
    "filesystem",
    "search",
    "repo",
    "execution",
    "validation",
    "analysis",
    "docs",
    "scaffold",
    "memory",
    "network",
    "browser",
)

#: How a written file is previewed in the panel.
_PREVIEW_KINDS = {
    ".html": "html",
    ".htm": "html",
    ".svg": "image",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
}

#: Keys a tool result may use to report the file it produced.
_PATH_KEYS = ("path", "file", "target", "output_path", "created", "output_file")

MAX_TOOL_OUTPUT_CHARS = 6_000
MAX_ARG_CHARS = 400
#: How many times we re-ask a gateway that answered with an empty completion.
EMPTY_TURN_RETRIES = 2


@dataclass(slots=True)
class ToolRun:
    """One executed tool call, exactly as the panel renders it."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    output: str
    duration_ms: float
    step: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "summary": self.summary,
            "output": self.output,
            "duration_ms": round(self.duration_ms, 1),
            "step": self.step,
        }


@dataclass(slots=True)
class Timing:
    """Where the wall clock went.  ``think`` is the wait before the first token,
    ``gen`` is the time spent writing visible output, ``tool`` is execution."""

    think_ms: float = 0.0
    gen_ms: float = 0.0
    tool_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.think_ms + self.gen_ms + self.tool_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "think_ms": round(self.think_ms),
            "gen_ms": round(self.gen_ms),
            "tool_ms": round(self.tool_ms),
            "total_ms": round(self.total_ms),
        }


@dataclass(slots=True)
class StreamTurn:
    """One streamed model turn plus the timestamps the timers need."""

    text: str = ""
    reasoning: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    first_token_at: float | None = None
    ended_at: float = 0.0


@dataclass(slots=True)
class WebChatResult:
    content: str
    reasoning: str = ""
    timing: Timing = field(default_factory=Timing)
    tools: list[ToolRun] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "reasoning": self.reasoning,
            "timing": self.timing.to_dict(),
            "tools": [tool.to_dict() for tool in self.tools],
            "files": self.files,
            "steps": self.steps,
            "usage": self.usage,
            "model": self.model,
            "provider": self.provider,
        }


def _safe_args(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Trim tool arguments for transport: the UI shows them, not the model."""

    safe: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str):
            safe[key] = value if len(value) <= MAX_ARG_CHARS else value[:MAX_ARG_CHARS] + "…"
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        else:
            text = str(value)
            safe[key] = text if len(text) <= MAX_ARG_CHARS else text[:MAX_ARG_CHARS] + "…"
    return safe


class WebAgent:
    """A streaming, tool-using agent bound to one workspace.

    One instance is cached per panel process and reused across messages, so the
    registry, memory store and provider client are built once.  Call
    :meth:`chat` per user message; it is stateless with respect to the
    transcript (the caller owns history).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        paths: AppPaths,
        categories: tuple[str, ...] = WEB_TOOL_CATEGORIES,
        max_steps: int = 12,
        auto_approve: bool = True,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.categories = categories
        self.max_steps = max_steps
        self.auto_approve = auto_approve
        self.project_root = Path(paths.cwd).resolve()
        self.registry: ToolRegistry | None = None
        self.executor: ToolExecutor | None = None
        self.context: ToolContext | None = None
        self.provider: Any = None
        self.provider_name: str = ""
        self._ready = False

    #: Sent when a turn comes back empty: some auto-routing gateways answer with
    #: reasoning only and drop the completion.
    NUDGE = (
        "Your previous response was empty. Continue now and deliver the result: "
        "either call the tool that does the work (for example write_file) or write the "
        "answer directly. Do not repeat the reasoning."
    )
    #: Second stage: the tool channel is evidently unusable on this run, so ask
    #: for the work as plain text. The panel previews fenced HTML/SVG blocks
    #: live, so the user still gets something runnable instead of an apology.
    NUDGE_NO_TOOLS = (
        "Tool calling is unavailable on this turn. Deliver the result as plain text: "
        "if the task is to create or change a file, output its COMPLETE content inside "
        "a single fenced code block and name the path in one line above it. "
        "Do not summarise, do not abbreviate, do not say 'add the rest here'."
    )

    # -- lifecycle --------------------------------------------------------- #
    @property
    def fingerprint(self) -> str:
        """Identity used to decide whether a cached agent is still valid."""

        settings = self.settings
        policy = settings.policy
        return "|".join(
            (
                str(self.project_root),
                str(getattr(settings, "provider", "")),
                str(getattr(settings.active_provider, "model", "")),
                str(getattr(settings.active_provider, "base_url", "")),
                str(policy.read_only),
                str(policy.dry_run),
                str(getattr(settings.browser, "enabled", False)),
                str(getattr(settings.vision, "enabled", False)),
                ",".join(self.categories),
            )
        )

    async def setup(self) -> WebAgent:
        """Build the subsystems.  Idempotent and cheap after the first call."""

        if self._ready:
            return self
        settings = self.settings

        browser_enabled = (
            bool(getattr(settings.browser, "enabled", False)) and not settings.policy.read_only
        )
        registry = build_default_registry(settings, include_browser=browser_enabled)
        for tool in list(registry.all()):
            if tool.category not in self.categories:
                registry.withhold(
                    tool.name, f"category {tool.category!r} is not exposed in the web panel"
                )
        self.registry = registry

        # A journal keeps the event bus off the memory-growth path while still
        # leaving an audit trail of what the panel did to the workspace.
        events = EventBus(journal=self.paths.project_dir / "runs" / "web-events.jsonl")

        from unknown_agent.ui.prompts import Prompter

        prompter = Prompter(assume_yes=self.auto_approve, non_interactive=True)

        context = ToolContext(
            settings=settings,
            paths=self.paths,
            project_root=self.project_root,
            events=events,
            prompter=prompter,
            run_id=f"web-{uuid.uuid4().hex[:8]}",
            artifacts_dir=self.paths.artifacts_dir,
            backups_dir=self.paths.backups_dir,
            cache_dir=self.paths.project_cache_dir,
            extra=self._tool_extra(),
        )
        try:
            from unknown_agent.memory.store import MemoryStore

            context.memory = MemoryStore(
                self.paths.project_memory_dir / "memory.db",
                fts_enabled=settings.memory.fts_enabled,
            )
        except Exception:  # pragma: no cover - memory is a nice-to-have here
            log.debug("web agent: memory store unavailable", exc_info=True)
        self.context = context

        self.executor = ToolExecutor(
            registry=registry,
            settings=settings,
            context=context,
            policy=PolicyEngine(settings, project_root=self.project_root, prompter=prompter),
        )
        # Imported here (not at module scope) so tests and the provider registry
        # can swap the factory: the panel must always use the live one.
        from unknown_agent.providers.registry import build_provider

        self.provider_name = str(getattr(settings, "provider", "") or "")
        self.provider = build_provider(settings.active_provider, name=self.provider_name)
        self._ready = True
        return self

    async def aclose(self) -> None:
        """Release the provider client when this agent is dropped from cache."""

        close = getattr(self.provider, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # pragma: no cover - best effort teardown
                log.debug("web agent: provider close failed", exc_info=True)
        self._ready = False

    def _tool_extra(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if getattr(self.settings.vision, "enabled", False):
            try:
                from unknown_agent.vision.service import VisionService

                extra["vision_service"] = VisionService(self.settings)
            except Exception:  # pragma: no cover - optional extra
                log.debug("web agent: vision service unavailable", exc_info=True)
        return extra

    # -- prompts ----------------------------------------------------------- #
    @property
    def tool_names(self) -> list[str]:
        return list(self.registry.names()) if self.registry else []

    def system_prompt(self, *, system_extra: str = "", tools_enabled: bool = True) -> str:
        cwd = str(self.project_root)
        lines = [
            "You are Unknown Agent, running inside the user's workspace through the web panel.",
            f"The working directory is {cwd}. Relative paths resolve there.",
            "",
        ]
        if tools_enabled and self.tool_names:
            lines += [
                "You have REAL tools and you are expected to use them: "
                + ", ".join(self.tool_names)
                + ".",
                "",
                "Rules:",
                "1. NEVER say you cannot access files, the terminal, the system or SSH. You can: "
                "call the tool instead of describing what the user should do.",
                "2. To create or change a file call write_file (whole content) or apply_patch "
                "(surgical edit). Do not paste a full file into chat when you can write it — "
                "written files appear in the panel with a live preview.",
                "3. Read before you edit: list_files / search_code / read_file first.",
                "4. Verify with shell_run, test_run or syntax_check when it is cheap.",
                "5. Never spend steps checking the environment (python3 --version, which "
                "node, ls of the workspace): assume a normal Linux box and do the work.",
                "6. Prefer write_file over shell heredocs; prefer apply_patch for edits.",
                "7. Finish in at most 4 steps, then answer in one or two sentences: what "
                "you did and where.",
                "8. Plain questions need no tools — just answer, concisely.",
                "9. Answer in the user's language.",
            ]
        else:
            lines += [
                "This session has NO tool-calling channel (the active model or provider does not "
                "support it), so you cannot write files yourself. Say so in one short sentence "
                "and then deliver the COMPLETE file content inside a single fenced code block — "
                "never a partial sketch, never 'add the rest here'. The panel renders a live "
                "preview of HTML blocks automatically.",
                "Answer in the user's language.",
            ]
        if system_extra:
            lines += ["", system_extra]
        return "\n".join(lines)

    # -- tool schemas ------------------------------------------------------ #
    def tool_schemas(self) -> list[ToolSchema]:
        if not self.registry:
            return []
        if not bool(getattr(self.provider, "supports_tools", False)):
            return []
        if self.settings.policy.read_only:
            return []
        return self.registry.schemas()

    # -- main loop --------------------------------------------------------- #
    async def chat(
        self,
        *,
        task: str,
        emit: Any,
        history: list[dict[str, str]] | None = None,
        images: list[str] | None = None,
        extra: str = "",
        system_extra: str = "",
        should_stop: Any = None,
    ) -> WebChatResult:
        """Run one user message to completion, streaming every phase."""

        await self.setup()
        assert self.registry is not None and self.executor is not None
        stop = should_stop or (lambda: False)

        schemas = self.tool_schemas()
        prompt = self.system_prompt(system_extra=system_extra, tools_enabled=bool(schemas))
        messages: list[Message] = [Message.system(prompt)]
        for item in history or []:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role in {"user", "assistant"} and content:
                messages.append(Message(role=role, content=content))  # type: ignore[arg-type]
        user_text = f"{task}\n\n{extra}" if extra else task
        messages.append(Message(role="user", content=user_text, images=list(images or [])))

        await emit(
            {
                "type": "tools",
                "tools": [schema.name for schema in schemas],
                "cwd": str(self.project_root),
            }
        )

        timing = Timing()
        runs: list[ToolRun] = []
        files: list[dict[str, Any]] = []
        seen_files: set[str] = set()
        visible_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        steps = 0
        empty_turns = 0

        for step in range(1, self.max_steps + 1):
            if stop():
                break
            steps = step
            await emit({"type": "phase", "phase": "thinking", "step": step})
            step_start = time.perf_counter()
            turn = await self._stream_once(messages, schemas, emit, step)
            text, reasoning, calls = turn.text, turn.reasoning, turn.calls
            if turn.usage:
                for key, value in turn.usage.items():
                    usage[key] = int(usage.get(key, 0)) + int(value)
            if reasoning:
                reasoning_parts.append(reasoning)
            if text:
                visible_parts.append(text)

            # think = wait for the first visible token; gen = time writing output.
            first_token = turn.first_token_at or turn.ended_at
            timing.think_ms += (first_token - step_start) * 1000
            timing.gen_ms += (turn.ended_at - first_token) * 1000

            if calls:
                empty_turns = 0
            else:
                # The turn should have produced the answer.  Auto-routing
                # gateways sometimes return an empty completion (or reasoning
                # only); ask again instead of leaving an empty bubble.  The
                # second attempt drops the tool channel: several gateways ignore
                # tools but happily write the whole file as a code block, and
                # the panel previews it live.
                if text.strip():
                    break
                empty_turns += 1
                if empty_turns > EMPTY_TURN_RETRIES or stop():
                    break
                if empty_turns >= 2:
                    schemas = []
                    await emit({"type": "phase", "phase": "generating", "step": step})
                messages.append(
                    Message.user(self.NUDGE_NO_TOOLS if empty_turns >= 2 else self.NUDGE)
                )
                continue

            # -- act: run every call through the policy-checked executor ---- #
            messages.append(Message.assistant(content=text, tool_calls=calls))
            await emit({"type": "phase", "phase": "tools", "step": step})
            for call in calls:
                await emit(
                    {
                        "type": "tool",
                        "status": "start",
                        "step": step,
                        "id": call.id,
                        "name": call.name,
                        "arguments": _safe_args(call.arguments),
                    }
                )
            tool_started = time.perf_counter()
            results = await self.executor.execute_many(calls)
            timing.tool_ms += (time.perf_counter() - tool_started) * 1000

            for call, result in zip(calls, results, strict=False):
                rendered = result.render(max_chars=MAX_TOOL_OUTPUT_CHARS)
                summary = (result.output or result.error or "").strip().splitlines()
                head = summary[0][:300] if summary else ""
                await emit(
                    {
                        "type": "tool",
                        "status": "end",
                        "step": step,
                        "id": call.id,
                        "name": call.name,
                        "ok": bool(result.ok),
                        "summary": head,
                        "output": rendered,
                        "duration_ms": round(result.duration_ms, 1),
                    }
                )
                runs.append(
                    ToolRun(
                        name=call.name,
                        arguments=_safe_args(call.arguments),
                        ok=bool(result.ok),
                        summary=head,
                        output=rendered,
                        duration_ms=result.duration_ms,
                        step=step,
                    )
                )
                messages.append(Message.tool_result(call.id, rendered, name=call.name))
                for artifact in self._artifacts(call, result):
                    if artifact["name"] in seen_files:
                        continue
                    seen_files.add(artifact["name"])
                    files.append(artifact)
                    await emit({"type": "artifact", **artifact})

        content = "\n\n".join(part for part in visible_parts if part).strip()

        # Some auto-routing models stop after the tool calls and leave the user
        # staring at an empty bubble. Ask once, without tools, for the closing
        # words; if that also comes back empty, summarise what actually ran.
        if not content and runs and not stop():
            steps += 1
            await emit({"type": "phase", "phase": "thinking", "step": steps})
            messages.append(
                Message(
                    role="user",
                    content=(
                        "You have finished using tools. Now answer the user directly in their "
                        "own language: one or two sentences saying what you did, which file you "
                        "created or changed, or what is missing. Do not call any tool."
                    ),
                )
            )
            try:
                closing = await self._stream_once(messages, [], emit, steps)
                if closing.text.strip():
                    content = closing.text.strip()
                    visible_parts.append(content)
            except Exception:  # pragma: no cover - the run already succeeded
                log.debug("web agent: closing summary failed", exc_info=True)

        if not content and runs:
            content = self._fallback_summary(runs, files)
            await emit({"type": "delta", "step": steps, "delta": content})
        elif not content:
            content = (
                "El proveedor devolvió una respuesta vacía tras "
                f"{max(1, steps)} intento(s). Reintenta, o cambia de modelo en el panel."
            )
            await emit({"type": "delta", "step": steps, "delta": content})

        return WebChatResult(
            content=content,
            reasoning="\n\n".join(part for part in reasoning_parts if part).strip(),
            timing=timing,
            tools=runs,
            files=files,
            steps=steps,
            usage=usage,
            model=str(getattr(self.provider, "model", "") or ""),
            provider=str(getattr(self.provider, "name", "") or self.provider_name),
        )

    @staticmethod
    def _fallback_summary(runs: list[ToolRun], files: list[dict[str, Any]]) -> str:
        """Last resort: say what ran, in the user's language, in two lines."""

        names = []
        for run in runs:
            if run.name not in names:
                names.append(run.name)
        written = [f"`{item['name']}`" for item in files]
        parts = [f"Ejecuté {len(runs)} llamada(s) a herramientas ({', '.join(names[:6])})."]
        if written:
            parts.append("Archivos escritos: " + ", ".join(written) + ".")
        parts.append(
            "El modelo no dejó texto final; revisa las tarjetas de herramientas de arriba "
            "para ver la salida completa."
        )
        return " ".join(parts)

    # -- streaming --------------------------------------------------------- #
    async def _stream_once(
        self,
        messages: list[Message],
        schemas: list[ToolSchema],
        emit: Any,
        step: int,
    ) -> StreamTurn:
        """Stream one model turn: reasoning, visible text and tool calls."""

        assert self.provider is not None
        profile = self.settings.active_provider
        request = ModelRequest(
            messages=messages,
            tools=schemas,
            temperature=float(
                getattr(profile, "temperature", None)
                or getattr(self.provider, "temperature", 0.7)
                or 0.7
            ),
            max_output_tokens=int(
                getattr(profile, "max_output_tokens", None)
                or getattr(self.provider, "max_output_tokens", 4096)
                or 4096
            ),
            reasoning=self._reasoning_params(),
            metadata={"surface": "web-panel"},
        )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: dict[str, int] = {}
        first_token_at: float | None = None
        generating = False

        async def start_generating() -> None:
            """Flip the panel from the thinking window to the answer, once."""

            nonlocal generating
            if generating:
                return
            generating = True
            await emit({"type": "phase", "phase": "generating", "step": step})

        def note_output() -> None:
            nonlocal first_token_at
            if first_token_at is None:
                first_token_at = time.perf_counter()

        if bool(getattr(self.provider, "supports_streaming", False)):
            async for chunk in self.provider.stream(request):
                error = getattr(chunk, "error", None)
                if error:
                    raise RuntimeError(str(error))
                reasoning = getattr(chunk, "reasoning", "") or ""
                if reasoning:
                    reasoning_parts.append(reasoning)
                    await emit({"type": "reasoning", "step": step, "delta": reasoning})
                delta = getattr(chunk, "delta", "") or ""
                if delta:
                    note_output()
                    await start_generating()
                    text_parts.append(delta)
                    await emit({"type": "delta", "step": step, "delta": delta})
                call = getattr(chunk, "tool_call", None)
                if call is not None:
                    note_output()
                    calls.append(call)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = {
                        "prompt_tokens": int(getattr(chunk_usage, "prompt_tokens", 0) or 0),
                        "completion_tokens": int(getattr(chunk_usage, "completion_tokens", 0) or 0),
                        "total_tokens": int(getattr(chunk_usage, "total_tokens", 0) or 0),
                    }
        else:
            # Non-streaming fallback.  Every field is read defensively: a
            # vendor (or a test double) that answers with a partial payload
            # must still work instead of blowing up the panel.
            response = await self.provider.complete(request)
            if getattr(response, "error", None):
                raise RuntimeError(str(response.error))
            note_output()
            await start_generating()
            reasoning = getattr(response, "reasoning", "") or ""
            if reasoning:
                reasoning_parts.append(reasoning)
                await emit({"type": "reasoning", "step": step, "delta": reasoning})
            content = getattr(response, "content", "") or ""
            if content:
                text_parts.append(content)
                await emit({"type": "delta", "step": step, "delta": content})
            calls = list(getattr(response, "tool_calls", None) or [])
            usage_obj = getattr(response, "usage", None)
            usage = (
                {
                    "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                    "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
                }
                if usage_obj is not None
                else {}
            )

        return StreamTurn(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            calls=calls,
            usage=usage,
            first_token_at=first_token_at,
            ended_at=time.perf_counter(),
        )

    def _reasoning_params(self) -> dict[str, Any]:
        """Vendor-neutral reasoning knobs from settings, when configured."""

        effort = str(getattr(self.settings.agent, "reasoning_effort", "") or "")
        if not effort:
            return {}
        try:
            from unknown_agent.reasoning import resolve_reasoning

            resolved = resolve_reasoning(self.settings, effort=effort)
            return resolved.to_dict() if hasattr(resolved, "to_dict") else dict(resolved)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover - reasoning is best-effort
            log.debug("web agent: reasoning params unavailable", exc_info=True)
            return {"effort": effort}

    # -- artifacts --------------------------------------------------------- #
    def _artifacts(self, call: ToolCall, result: ToolResult) -> list[dict[str, Any]]:
        """Files a tool result reports as written, as panel-previewable cards."""

        candidates: list[str] = []
        data = result.data or {}
        for key in _PATH_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
        if isinstance(data.get("files"), list):
            candidates += [str(item) for item in data["files"] if isinstance(item, str)]
        candidates += [str(item) for item in (result.artifacts or []) if isinstance(item, str)]
        if not candidates:
            return []

        root = self.project_root
        out: list[dict[str, Any]] = []
        for raw in candidates:
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            try:
                path = path.resolve()
            except OSError:  # pragma: no cover - exotic path
                continue
            if not path.is_file() or not path.is_relative_to(root):
                continue
            rel = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            out.append(
                {
                    "url": f"/api/files/{quote(rel)}",
                    "name": rel,
                    "kind": _PREVIEW_KINDS.get(suffix, "text"),
                    "size": path.stat().st_size,
                    "tool": call.name,
                }
            )
        return out


async def run_web_chat(
    *, settings: Settings, paths: AppPaths, task: str, emit: Any, **kwargs: Any
) -> WebChatResult:
    """Convenience entry point (tests, scripts)."""

    agent = WebAgent(settings=settings, paths=paths)
    try:
        await agent.setup()
        return await agent.chat(task=task, emit=emit, **kwargs)
    finally:
        await asyncio.sleep(0)
