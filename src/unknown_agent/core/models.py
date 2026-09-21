"""Domain models shared by every subsystem.

These dataclasses are the vocabulary of the agent: messages, tool calls,
observations, plans, steps, verification results and run state.  They are
provider-agnostic and serialisation-friendly (``to_dict``) so a run journal can be
replayed, exported or resumed without importing a provider SDK.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

# --------------------------------------------------------------------------- #
# messaging
# --------------------------------------------------------------------------- #
Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str | None = None

    @staticmethod
    def new(name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
        return ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments or {})

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    images: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        return payload

    @staticmethod
    def system(content: str) -> Message:
        return Message(role="system", content=content)

    @staticmethod
    def user(content: str) -> Message:
        return Message(role="user", content=content)

    @staticmethod
    def assistant(content: str = "", tool_calls: list[ToolCall] | None = None) -> Message:
        return Message(role="assistant", content=content, tool_calls=tool_calls or [])

    @staticmethod
    def tool_result(tool_call_id: str, content: str, name: str | None = None) -> Message:
        return Message(role="tool", content=content, tool_call_id=tool_call_id, name=name)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
class RiskLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


@dataclass(slots=True)
class ToolResult:
    """What a tool returns.  ``ok`` is about execution, not about the domain."""

    ok: bool
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    truncated: bool = False
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def success(output: str = "", *, metadata: dict[str, Any] | None = None, **data: Any) -> ToolResult:
        return ToolResult(ok=True, output=output, data=data, metadata=dict(metadata or {}))

    @staticmethod
    def failure(error: str, *, output: str = "", metadata: dict[str, Any] | None = None) -> ToolResult:
        return ToolResult(ok=False, output=output, error=error, metadata=dict(metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("metadata", None)
        return payload

    def render(self, *, max_chars: int = 6000) -> str:
        body = self.output if self.ok else (self.error or self.output or "tool failed")
        if len(body) > max_chars:
            return body[:max_chars] + f"\n... [{len(body) - max_chars} more characters truncated]"
        return body


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(slots=True)
class PlanStep:
    title: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    evidence: list[str] = field(default_factory=list)
    notes: str = ""
    id: str = field(default_factory=lambda: f"s{ uuid.uuid4().hex[:6]}")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(slots=True)
class Plan:
    goal: str
    steps: list[PlanStep]
    deliverables: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    revision: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def current_step(self) -> PlanStep | None:
        for step in self.steps:
            if step.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}:
                return step
        return None

    @property
    def completed(self) -> bool:
        return all(step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED} for step in self.steps) and bool(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "revision": self.revision,
            "steps": [step.to_dict() for step in self.steps],
            "deliverables": self.deliverables,
            "risks": self.risks,
            "verification": self.verification,
            "assumptions": self.assumptions,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> Plan:
        steps = [
            PlanStep(
                id=item.get("id") or f"s{uuid.uuid4().hex[:6]}",
                title=item.get("title", "step"),
                description=item.get("description", ""),
                tools=list(item.get("tools") or []),
                acceptance=list(item.get("acceptance") or []),
                status=StepStatus(item.get("status", "pending")),
                evidence=list(item.get("evidence") or []),
                notes=item.get("notes", ""),
            )
            for item in payload.get("steps", [])
        ]
        return Plan(
            goal=payload.get("goal", ""),
            steps=steps,
            deliverables=list(payload.get("deliverables") or []),
            risks=list(payload.get("risks") or []),
            verification=list(payload.get("verification") or []),
            assumptions=list(payload.get("assumptions") or []),
            revision=int(payload.get("revision", 0)),
        )


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
class GateStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(slots=True)
class GateResult:
    name: str
    status: GateStatus = GateStatus.PENDING
    command: str | None = None
    exit_code: int | None = None
    duration_s: float = 0.0
    summary: str = ""
    output: str = ""
    required: bool = False
    artifacts: list[str] = field(default_factory=list)
    fix_hint: str | None = None

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASSED

    @property
    def blocking(self) -> bool:
        return self.required and self.status in {GateStatus.FAILED, GateStatus.ERROR}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload.pop("output", None)
        payload["output_preview"] = self.output[:2000]
        return payload


@dataclass(slots=True)
class VerificationReport:
    results: list[GateResult] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return all(not result.blocking for result in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [result for result in self.results if result.status in {GateStatus.FAILED, GateStatus.ERROR}]

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [result for result in self.results if result.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "created_at": self.created_at,
            "results": [result.to_dict() for result in self.results],
        }


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class RunBudget:
    max_steps: int = 60
    max_tokens: int = 900_000
    max_wall_clock_s: float = 2700.0
    steps_used: int = 0
    tokens_used: int = 0
    started_at: float = field(default_factory=time.time)
    tool_calls: int = 0

    @property
    def wall_clock_s(self) -> float:
        return time.time() - self.started_at

    @property
    def steps_exhausted(self) -> bool:
        return self.steps_used >= self.max_steps

    @property
    def tokens_exhausted(self) -> bool:
        return self.tokens_used >= self.max_tokens

    @property
    def time_exhausted(self) -> bool:
        return self.wall_clock_s >= self.max_wall_clock_s

    @property
    def exhausted(self) -> bool:
        return self.steps_exhausted or self.tokens_exhausted or self.time_exhausted

    def reason(self) -> str | None:
        if self.steps_exhausted:
            return f"step budget exhausted ({self.steps_used}/{self.max_steps})"
        if self.tokens_exhausted:
            return f"token budget exhausted ({self.tokens_used:,}/{self.max_tokens:,})"
        if self.time_exhausted:
            return f"wall-clock budget exhausted ({self.wall_clock_s:.0f}s/{self.max_wall_clock_s:.0f}s)"
        return None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["wall_clock_s"] = round(self.wall_clock_s, 2)
        payload["exhausted"] = self.exhausted
        return payload


@dataclass(slots=True)
class RunState:
    """Everything needed to resume, audit or explain a run."""

    id: str
    task: str
    mode: str = "auto"
    status: RunStatus = RunStatus.PENDING
    step: int = 0
    plan: Plan | None = None
    budget: RunBudget = field(default_factory=RunBudget)
    verification: VerificationReport | None = None
    files_changed: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    summary: str = ""
    blockers: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cwd: str = ""
    provider: str = ""
    model: str = ""
    session_id: str | None = None
    artifacts: list[str] = field(default_factory=list)

    @staticmethod
    def new(task: str, *, cwd: str = "", provider: str = "", model: str = "", mode: str = "auto") -> RunState:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return RunState(
            id=f"run-{stamp}-{uuid.uuid4().hex[:6]}",
            task=task,
            mode=mode,
            cwd=cwd,
            provider=provider,
            model=model,
        )

    @property
    def duration_s(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def finish(self, status: RunStatus, *, summary: str = "") -> None:
        self.status = status
        self.summary = summary or self.summary
        self.finished_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "mode": self.mode,
            "status": self.status.value,
            "step": self.step,
            "plan": self.plan.to_dict() if self.plan else None,
            "budget": self.budget.to_dict(),
            "verification": self.verification.to_dict() if self.verification else None,
            "files_changed": self.files_changed,
            "commands_run": self.commands_run,
            "summary": self.summary,
            "blockers": self.blockers,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "session_id": self.session_id,
            "artifacts": self.artifacts,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> RunState:
        budget_payload = payload.get("budget") or {}
        budget = RunBudget(
            max_steps=int(budget_payload.get("max_steps", 60)),
            max_tokens=int(budget_payload.get("max_tokens", 900_000)),
            max_wall_clock_s=float(budget_payload.get("max_wall_clock_s", 2700.0)),
            steps_used=int(budget_payload.get("steps_used", 0)),
            tokens_used=int(budget_payload.get("tokens_used", 0)),
            tool_calls=int(budget_payload.get("tool_calls", 0)),
        )
        state = RunState(
            id=payload.get("id", "run-unknown"),
            task=payload.get("task", ""),
            mode=payload.get("mode", "auto"),
            status=RunStatus(payload.get("status", "pending")),
            step=int(payload.get("step", 0)),
            plan=Plan.from_dict(payload["plan"]) if payload.get("plan") else None,
            budget=budget,
            files_changed=list(payload.get("files_changed") or []),
            commands_run=list(payload.get("commands_run") or []),
            summary=payload.get("summary", ""),
            blockers=list(payload.get("blockers") or []),
            cwd=payload.get("cwd", ""),
            provider=payload.get("provider", ""),
            model=payload.get("model", ""),
            session_id=payload.get("session_id"),
            artifacts=list(payload.get("artifacts") or []),
        )
        state.started_at = float(payload.get("started_at", time.time()))
        state.finished_at = payload.get("finished_at")
        return state


@dataclass(slots=True)
class AgentDecision:
    """One model turn: what it said, what it wants to do, why it is done."""

    thought: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    final: bool = False
    answer: str = ""
    note: str = ""


@dataclass(slots=True)
class StepRecord:
    index: int
    decision: AgentDecision
    results: list[ToolResult] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def summary(self) -> str:
        """One line describing what the step did and whether it worked.

        Used by self-healing diagnostics and by run reports, so it must never raise
        and must be honest about missing results.
        """

        tools = ", ".join(call.name for call in self.decision.tool_calls) or "no tool call"
        if not self.results:
            outcome = "no result recorded"
        elif all(result.ok for result in self.results):
            outcome = "ok"
        else:
            first_error = next((result.error for result in self.results if not result.ok and result.error), None)
            outcome = f"failed ({first_error or 'see logs'})"
        return f"{tools} -> {outcome}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "thought": self.decision.thought,
            "summary": self.summary,
            "tool_calls": [call.to_dict() for call in self.decision.tool_calls],
            "results": [result.to_dict() for result in self.results],
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


__all__ = [
    "Role",
    "Message",
    "ToolCall",
    "ToolResult",
    "RiskLevel",
    "StepStatus",
    "PlanStep",
    "Plan",
    "GateStatus",
    "GateResult",
    "VerificationReport",
    "RunStatus",
    "RunBudget",
    "RunState",
    "AgentDecision",
    "StepRecord",
]
