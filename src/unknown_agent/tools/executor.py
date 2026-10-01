"""Tool executor: the only path from a decision to a side effect.

Responsibilities, in order:

1. resolve the tool (unknown names produce a *useful* error the model can act on);
2. consult the policy engine and, when needed, the user;
3. run the tool with its own timeout, optionally in parallel for independent
   read-only calls, while preserving result order;
4. emit ``tool.started`` / ``tool.finished`` / ``tool.denied`` / ``tool.failed``
   events for the UI and the run journal;
5. truncate oversized observations so one noisy command cannot blow up the
   context window, keeping head+tail with an explicit truncation marker;
6. record telemetry counters and update the run journal's tool ledger.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.core.models import RiskLevel, ToolCall, ToolResult
from unknown_agent.observability.logging import get_logger
from unknown_agent.tools.base import Tool, ToolContext
from unknown_agent.tools.policy import PolicyEngine
from unknown_agent.tools.registry import ToolRegistry

log = get_logger("tools.executor")


@dataclass(slots=True)
class ExecutionRecord:
    call: ToolCall
    result: ToolResult
    tool: str
    risk: str
    denied: bool = False
    policy_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.call.arguments,
            "risk": self.risk,
            "ok": self.result.ok,
            "denied": self.denied,
            "policy_reason": self.policy_reason,
            "duration_ms": round(self.result.duration_ms, 1),
            "error": self.result.error,
            "output_preview": self.result.output[:1500],
        }


@dataclass(slots=True)
class ToolExecutor:
    registry: ToolRegistry
    settings: Settings
    context: ToolContext
    policy: PolicyEngine
    max_output_chars: int = 12_000
    max_parallel: int = 4
    records: list[ExecutionRecord] = field(default_factory=list)

    # -- public API --------------------------------------------------------
    async def execute_many(self, calls: list[ToolCall]) -> list[ToolResult]:
        """Run calls, preserving order, parallelising only safe read-only groups."""

        if not calls:
            return []
        results: list[ToolResult | None] = [None] * len(calls)
        batch: list[tuple[int, ToolCall]] = []
        for index, call in enumerate(calls):
            tool = self.registry.maybe_get(call.name)
            if tool is not None and tool.risk is RiskLevel.READ and tool.concurrency_safe:
                batch.append((index, call))
                continue
            if batch:
                await self._run_batch(batch, results)
                batch = []
            results[index] = await self.execute(call)
        if batch:
            await self._run_batch(batch, results)
        return [result or ToolResult.failure("tool did not run") for result in results]

    async def _run_batch(self, batch: list[tuple[int, ToolCall]], results: list[ToolResult | None]) -> None:
        semaphore = asyncio.Semaphore(max(1, self.max_parallel))

        async def guarded(index: int, call: ToolCall) -> None:
            async with semaphore:
                results[index] = await self.execute(call)

        await asyncio.gather(*(guarded(index, call) for index, call in batch))

    async def execute(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        tool = self.registry.maybe_get(call.name)
        if tool is None:
            suggestion = _closest(call.name, self.registry.names())
            reason = self.registry.withholder_reason(call.name)
            if reason:
                result = ToolResult.failure(
                    f"Tool {call.name!r} is unavailable in this session: {reason}",
                    output=f"Available tools: {', '.join(self.registry.names())}",
                )
            else:
                result = ToolResult.failure(
                    f"Unknown tool {call.name!r}." + (f" Did you mean {suggestion!r}?" if suggestion else ""),
                    output=f"Available tools: {', '.join(self.registry.names())}",
                )
            self._record(call, result, tool=None)
            self._emit("tool.failed", call, result)
            return result

        arguments = dict(call.arguments or {})
        decision = self.policy.evaluate(tool, arguments)
        if not decision.allowed:
            result = ToolResult.failure(
                f"Denied by policy: {decision.denied_reason}",
                output=(
                    f"{decision.denied_reason}\n"
                    "Do not retry this call; choose another approach or ask the user to change policy."
                ),
            )
            self._record(call, result, tool=tool, denied=True, reason=decision.denied_reason)
            self._emit("tool.denied", call, result, reason=decision.denied_reason)
            return result

        if decision.requires_confirmation:
            decision = self.policy.confirm(tool, arguments, decision)
            if not decision.allowed:
                reason = decision.denied_reason.rstrip(".").capitalize()
                result = ToolResult.failure(
                    reason + ".",
                    output=(
                        f"{decision.denied_reason}\n"
                        "Nothing was executed. Choose a safer alternative, explain what you need, "
                        "or ask the user to approve it."
                    ),
                )
                self._record(call, result, tool=tool, denied=True, reason=decision.denied_reason)
                self._emit("tool.denied", call, result, reason=decision.denied_reason)
                return result

        if decision.dry_run:
            result = ToolResult.success(
                f"[dry-run] {tool.summarise_call(arguments)} was not executed.",
                dry_run=True,
            )
            self._record(call, result, tool=tool)
            self._emit("tool.finished", call, result, dry_run=True)
            return result

        self._emit("tool.started", call, None, risk=tool.risk.value, category=tool.category)
        self.context.emit("tool.requested", tool.summarise_call(arguments), **{"tool": tool.name, "arguments": _safe_arguments(arguments)})
        await asyncio.sleep(0)  # yield so UI can paint the call before a slow tool blocks

        try:
            result = await tool.execute(self.context, arguments)
        except asyncio.CancelledError:
            self._emit("tool.failed", call, None, reason="cancelled")
            raise

        result.output = _truncate(result.output, self.max_output_chars)
        result.error = _truncate(result.error or "", 2_000) or None
        self._record(call, result, tool=tool)
        self._emit("tool.finished" if result.ok else "tool.failed", call, result, risk=tool.risk.value)
        if (time.perf_counter() - started) > 60:  # pragma: no cover - slow tool notice
            log.info("slow tool %s took %.1fs", tool.name, time.perf_counter() - started)
        return result

    # -- internals ---------------------------------------------------------
    def _record(self, call: ToolCall, result: ToolResult, *, tool: Tool | None, denied: bool = False, reason: str = "") -> None:
        self.records.append(
            ExecutionRecord(
                call=call,
                result=result,
                tool=call.name,
                risk=tool.risk.value if tool else "unknown",
                denied=denied,
                policy_reason=reason,
            )
        )

    def _emit(self, event_type: str, call: ToolCall, result: ToolResult | None, **data: Any) -> None:
        payload = {"tool": call.name, "arguments": _safe_arguments(call.arguments), **data}
        if result is not None:
            payload["ok"] = result.ok
            payload["duration_ms"] = round(result.duration_ms, 1)
            if result.error:
                payload["error"] = result.error
            if result.data:
                payload["data"] = {key: value for key, value in result.data.items() if _is_small(value)}
        self.context.emit(event_type, _summarise(call, self.registry), **payload)

    def ledger(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.records]

    def stats(self) -> dict[str, Any]:
        ok = sum(1 for record in self.records if record.result.ok)
        denied = sum(1 for record in self.records if record.denied)
        return {
            "tool_calls": len(self.records),
            "succeeded": ok,
            "failed": len(self.records) - ok - denied,
            "denied": denied,
        }


def _summarise(call: ToolCall, registry: ToolRegistry) -> str:
    tool = registry.maybe_get(call.name)
    if tool is None:
        return call.name
    return f"{call.name}({', '.join(f'{k}={_short(v)}' for k, v in list(call.arguments.items())[:4])})"


def _short(value: Any) -> str:
    text = str(value).replace("\n", "\\n")
    return text[:48] + ("..." if len(text) > 48 else "")


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 400:
            out[key] = value[:400] + f"... [{len(value) - 400} more characters]"
        else:
            out[key] = value
    return out


def _is_small(value: Any) -> bool:
    if isinstance(value, (int, float, bool)) or value is None:
        return True
    if isinstance(value, str):
        return len(value) < 500
    if isinstance(value, (list, dict)):
        return len(value) < 20
    return False


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.55)
    tail = limit - head
    omitted = len(text) - limit
    return (
        text[:head]
        + f"\n\n... [{omitted} characters omitted by the tool executor - narrow the request "
        "or use a more specific tool to see the rest] ...\n\n"
        + text[-tail:]
    )


def _closest(name: str, candidates: list[str]) -> str | None:
    import difflib

    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


__all__ = ["ToolExecutor", "ExecutionRecord"]
