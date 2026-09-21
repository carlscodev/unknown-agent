"""Workers: task state, role boundaries, and the execution contract.

The conductor never talks to a model directly.  It creates a :class:`WorkItem`,
hands it to a :class:`Worker`, and gets back a :class:`WorkResult`.  The worker in
turn hands the item to an injected **executor** — normally the real agent loop, in
tests and offline mode a deterministic stand-in.  That seam is deliberate:

* orchestration logic (scheduling, budgets, merging, escalation, auditing) is
  testable without a network call or an API key;
* a role cannot exceed its remit, because the worker builds its tool subset,
  prompt and budget from the role card and the session policy, not from the task;
* the same worker code drives a solo run and a twelve-worker graph.

State machine (every transition is legal-checked and recorded):

    pending → queued → running → succeeded
                       running → failed → queued      (retry, attempts < max)
                       running → failed → blocked      (retries exhausted)
                       running → blocked               (dependency failed)
    any     → cancelled                                (budget exhausted / Ctrl-C)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from unknown_agent.orchestration.roles import Role, get_role
from unknown_agent.observability.logging import get_logger

log = get_logger("orchestration.worker")

__all__ = [
    "WorkState",
    "WorkItem",
    "WorkResult",
    "WorkerContext",
    "WorkExecutor",
    "Worker",
    "OfflineExecutor",
    "CallableExecutor",
    "LEGAL_TRANSITIONS",
    "OrchestrationError",
]


class OrchestrationError(RuntimeError):
    """An illegal orchestration operation (bad transition, missing dependency)."""


class WorkState(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in {WorkState.SUCCEEDED, WorkState.BLOCKED, WorkState.CANCELLED, WorkState.SKIPPED}

    @property
    def ok(self) -> bool:
        return self is WorkState.SUCCEEDED


LEGAL_TRANSITIONS: dict[WorkState, frozenset[WorkState]] = {
    WorkState.PENDING: frozenset({WorkState.QUEUED, WorkState.SKIPPED, WorkState.CANCELLED, WorkState.BLOCKED}),
    WorkState.QUEUED: frozenset({WorkState.RUNNING, WorkState.CANCELLED, WorkState.BLOCKED, WorkState.SKIPPED, WorkState.PENDING}),
    WorkState.RUNNING: frozenset({WorkState.SUCCEEDED, WorkState.FAILED, WorkState.CANCELLED, WorkState.BLOCKED}),
    WorkState.FAILED: frozenset({WorkState.QUEUED, WorkState.BLOCKED, WorkState.CANCELLED}),
    WorkState.SUCCEEDED: frozenset(),
    WorkState.BLOCKED: frozenset({WorkState.CANCELLED}),
    WorkState.CANCELLED: frozenset(),
    WorkState.SKIPPED: frozenset(),
}


@dataclass(slots=True)
class WorkItem:
    """One delegated unit of work."""

    id: str = field(default_factory=lambda: f"w{uuid.uuid4().hex[:8]}")
    role: str = "coder"
    task: str = ""
    context: str = ""
    depends_on: list[str] = field(default_factory=list)
    owned_paths: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    state: WorkState = WorkState.PENDING
    attempts: int = 0
    max_attempts: int = 3
    max_steps: int = 24
    priority: int = 5
    parallel_group: str = ""
    required: bool = True
    reasoning_mode: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    last_error: str = ""
    #: Filled by the conductor after execution.
    result: "WorkResult | None" = None

    # -- state ------------------------------------------------------------- #
    def transition(self, state: WorkState, *, note: str = "") -> None:
        if state is self.state:
            return
        allowed = LEGAL_TRANSITIONS.get(self.state, frozenset())
        if state not in allowed:
            raise OrchestrationError(f"illegal work transition {self.id}: {self.state.value} → {state.value}")
        previous = self.state
        self.state = state
        if state is WorkState.RUNNING:
            self.started_at = time.time()
            self.attempts += 1
        if state in {WorkState.SUCCEEDED, WorkState.FAILED, WorkState.BLOCKED, WorkState.CANCELLED, WorkState.SKIPPED}:
            self.finished_at = time.time()
        self.history.append(
            {
                "at": time.time(),
                "from": previous.value,
                "to": state.value,
                "note": note,
                "attempt": self.attempts,
            }
        )
        log.debug("work %s [%s] %s → %s %s", self.id, self.role, previous.value, state.value, note or "")

    @property
    def can_retry(self) -> bool:
        """Whether another attempt is both allowed and meaningful.

        Terminal states are terminal: a succeeded worker is not "retryable", and a
        cancelled or skipped one was a decision, not a failure.
        """

        if self.state in {WorkState.SUCCEEDED, WorkState.CANCELLED, WorkState.SKIPPED}:
            return False
        return self.attempts < self.max_attempts

    @property
    def attempts_left(self) -> int:
        """Attempts remaining, ignoring the current state.

        Verification needs this because the item it is judging is SUCCEEDED — the
        very state that makes :attr:`can_retry` false.  Whether another attempt is
        *available* is a different question from whether this item is currently
        retryable, and conflating them made every red gate terminal.
        """

        return max(0, self.max_attempts - self.attempts)

    def reopen(self, reason: str, *, to: WorkState = WorkState.QUEUED) -> None:
        """Take back a succeeded item because verification disagreed.

        ``transition`` keeps SUCCEEDED terminal — a worker may not un-succeed
        itself.  Reopening is a different act: the conductor does it on evidence (a
        gate came back red) and moves the item either back to the queue for another
        attempt or straight to BLOCKED when attempts are exhausted.  The reason is
        written into the history, so the audit trail shows *why* finished work was
        taken back rather than merely that it was.
        """

        if self.state is not WorkState.SUCCEEDED:
            raise OrchestrationError(f"cannot reopen {self.id}: it is {self.state.value}, not succeeded")
        if to not in {WorkState.QUEUED, WorkState.BLOCKED}:
            raise OrchestrationError(f"cannot reopen {self.id} into {to.value}")
        previous = self.state
        self.state = to
        self.history.append(
            {
                "at": time.time(),
                "from": previous.value,
                "to": to.value,
                "note": f"reopened: {reason}"[:200],
                "attempt": self.attempts,
            }
        )

    @property
    def duration_s(self) -> float:
        if not self.started_at:
            return 0.0
        return round((self.finished_at or time.time()) - self.started_at, 2)

    def role_card(self) -> Role:
        return get_role(self.role)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "task": self.task,
            "context": self.context,
            "depends_on": self.depends_on,
            "owned_paths": self.owned_paths,
            "outputs": self.outputs,
            "acceptance": self.acceptance,
            "state": self.state.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "max_steps": self.max_steps,
            "priority": self.priority,
            "parallel_group": self.parallel_group,
            "required": self.required,
            "duration_s": self.duration_s,
            "last_error": self.last_error,
            "history": self.history,
        }


@dataclass(slots=True)
class WorkResult:
    """What a worker handed back."""

    item_id: str
    role: str
    ok: bool = True
    summary: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    files_touched: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    steps_used: int = 0
    tokens_used: int = 0
    duration_s: float = 0.0
    error: str | None = None
    #: Structured payload for the conductor (gate reports, repo maps, plans).
    data: dict[str, Any] = field(default_factory=dict)
    #: What the worker wants the conductor to do next, if anything.
    follow_ups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "role": self.role,
            "ok": self.ok,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "files_touched": self.files_touched,
            "evidence": self.evidence,
            "steps_used": self.steps_used,
            "tokens_used": self.tokens_used,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            "data": self.data,
            "follow_ups": self.follow_ups,
        }


@dataclass(slots=True)
class WorkerContext:
    """Everything a worker is allowed to know and use."""

    project_root: str = "."
    task: str = ""
    blueprint_text: str = ""
    repo_map: str = ""
    conventions: list[str] = field(default_factory=list)
    skill_prompt: str = ""
    policy_summary: str = ""
    previous_results: dict[str, WorkResult] = field(default_factory=dict)
    settings: Any = None

    def digest(self, *, max_chars: int = 2400) -> str:
        parts: list[str] = []
        if self.blueprint_text:
            parts.append("PROJECT BLUEPRINT:\n" + self.blueprint_text[:1200])
        if self.repo_map:
            parts.append("REPOSITORY MAP:\n" + self.repo_map[:600])
        if self.conventions:
            parts.append("CONVENTIONS TO FOLLOW:\n" + "\n".join(f"- {item}" for item in self.conventions[:12]))
        if self.policy_summary:
            parts.append("PERMISSIONS:\n" + self.policy_summary[:300])
        if self.previous_results:
            lines = [f"- [{role}] {result.summary[:160]}" for role, result in self.previous_results.items()]
            parts.append("ALREADY DONE BY OTHER WORKERS:\n" + "\n".join(lines[:10]))
        text = "\n\n".join(parts)
        return text[:max_chars]


@runtime_checkable
class WorkExecutor(Protocol):
    """The seam between orchestration and execution."""

    async def execute(self, item: WorkItem, role: Role, context: WorkerContext) -> WorkResult:
        """Do the work and report honestly."""


class Worker:
    """Runs one :class:`WorkItem` inside its role's boundaries."""

    def __init__(
        self,
        role: Role | str,
        *,
        executor: WorkExecutor | None = None,
        context: WorkerContext | None = None,
        on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.role = role if isinstance(role, Role) else get_role(role)
        self.executor = executor
        self.context = context or WorkerContext()
        self._on_event = on_event

    # -- boundary enforcement --------------------------------------------- #
    def tool_subset(self, available: Iterable[str]) -> list[str]:
        """The tools this worker may use, given what the session actually has.

        A role's grant list is a *request*: the intersection with the session
        registry is what it gets.  ``deny_tools`` is applied last so a role can be
        neutered (read-only mode, ``--no-browser``) without editing the role card.
        """

        session = list(available)
        denied = set(self.role.deny_tools)
        granted = [tool for tool in self.role.tools if tool in session and tool not in denied]
        if not granted:
            # Never hand a worker an empty toolbelt: give it read access at minimum so
            # it can report "I cannot do this" with evidence instead of guessing.
            granted = [tool for tool in ("read_file", "list_files", "search_code") if tool in session]
        return granted

    def build_prompt(self, item: WorkItem) -> str:
        """The role's system prompt: mission, boundaries, contract, guidance."""

        lines = [
            f"You are the {self.role.label} worker inside Unknown Agent's orchestration.",
            f"MISSION: {self.role.mission}",
            "",
            f"TASK: {item.task}",
        ]
        if item.context:
            lines.append(f"CONTEXT: {item.context}")
        digest = self.context.digest()
        if digest:
            lines += ["", digest]
        if self.context.skill_prompt:
            lines += ["", self.context.skill_prompt[:1200]]
        lines += [
            "",
            "BOUNDARIES (hard):",
            f"- You may use only these tools: {', '.join(self.role.tools) or 'none'}.",
            f"- You may {'modify' if self.role.mutates else 'NOT modify'} files in the repository.",
        ]
        if item.owned_paths:
            lines.append(f"- You own these paths only: {', '.join(item.owned_paths)}.")
        lines.append("- Do not perform work that belongs to another role; report it as a follow-up instead.")
        if item.acceptance:
            lines += ["", "ACCEPTANCE (each must be provable):"] + [f"- {criterion}" for criterion in item.acceptance]
        if self.role.outputs:
            lines += ["", f"YOU MUST RETURN: {', '.join(self.role.outputs)}"]
        if self.role.guidance:
            lines += ["", f"CRAFT: {self.role.guidance}"]
        lines += [
            "",
            "Report honestly. If you could not complete the work, say exactly what blocked you and what "
            "evidence you have; a truthful failure is useful, a claimed success is not.",
        ]
        return "\n".join(lines)

    # -- execution --------------------------------------------------------- #
    async def run(self, item: WorkItem) -> WorkResult:
        started = time.time()
        if item.state is WorkState.PENDING:
            # The conductor queues before dispatching, but a worker used on its own
            # (a subagent, a test, ``ua orchestrate worker``) must not have to
            # reproduce that ritual — and the state machine forbids the shortcut.
            item.transition(WorkState.QUEUED, note="queued by worker")
        item.transition(WorkState.RUNNING, note=f"worker={self.role.id} attempt={item.attempts + 1}")
        self._emit("worker_started", item=item)
        if self.executor is None:
            result = _unexecuted_result(item, self.role)
        else:
            try:
                result = await self.executor.execute(item, self.role, self.context)
            except Exception as exc:  # noqa: BLE001 - a worker must never kill the conductor
                log.warning("worker %s raised %s: %s", self.role.id, type(exc).__name__, exc)
                result = WorkResult(
                    item_id=item.id,
                    role=self.role.id,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    summary=f"{self.role.label} failed with an unexpected error",
                )
        result.duration_s = round(time.time() - started, 2)
        item.result = result
        missing = [name for name in (item.outputs or list(self.role.outputs)) if name not in result.artifacts and name not in result.data]
        if result.ok and missing and self.role.required:
            result.ok = False
            result.error = f"worker did not produce required outputs: {', '.join(missing)}"
        if result.ok:
            item.last_error = ""
            item.transition(WorkState.SUCCEEDED, note=result.summary[:160])
        else:
            item.last_error = result.error or "worker reported failure"
            item.transition(WorkState.FAILED, note=item.last_error[:160])
        self._emit("worker_finished", item=item, ok=result.ok)
        return result

    def _emit(self, event: str, *, item: WorkItem, ok: bool | None = None) -> None:
        if self._on_event is None:
            return
        payload: dict[str, Any] = {
            "work_id": item.id,
            "role": self.role.id,
            "task": item.task[:120],
            "attempt": item.attempts,
            "state": item.state.value,
        }
        if ok is not None:
            payload["ok"] = ok
        try:
            self._on_event(event, payload)
        except Exception:  # pragma: no cover - an observer must never break the run
            log.debug("orchestration event sink failed for %s", event)


def _unexecuted_result(item: WorkItem, role: Role) -> WorkResult:
    """What a worker reports when no executor was wired up.

    This is not an error path: ``ua orchestrate plan --dry-run`` uses it to show
    exactly what would be delegated, with which tools, under which boundaries.
    """

    return WorkResult(
        item_id=item.id,
        role=role.id,
        ok=False,
        error="no executor wired (dry run)",
        summary=f"{role.label} would run: {item.task[:120]}",
        data={"dry_run": True, "tools": list(role.tools), "prompt_chars": 0},
    )


class OfflineExecutor:
    """A deterministic executor: no model, no network, real state transitions.

    Used by tests, by ``--provider offline``, and by ``ua orchestrate demo`` so the
    orchestration machinery can be exercised and shown end to end.  It produces
    plausible artefacts per role and honest summaries that say it was simulated.
    """

    def __init__(self, *, fail_roles: Sequence[str] = (), fail_once: Sequence[str] = ()) -> None:
        self.fail_roles = set(fail_roles)
        self.fail_once = set(fail_once)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, item: WorkItem, role: Role, context: WorkerContext) -> WorkResult:
        self.calls.append({"item": item.id, "role": role.id, "attempt": item.attempts})
        if role.id in self.fail_roles or (role.id in self.fail_once and item.attempts <= 1):
            return WorkResult(
                item_id=item.id,
                role=role.id,
                ok=False,
                error=f"simulated failure in {role.id}",
                summary=f"{role.label} failed (simulated)",
                steps_used=1,
                tokens_used=0,
            )
        artifacts = {name: f"<{name} produced by simulated {role.id}>" for name in role.outputs}
        return WorkResult(
            item_id=item.id,
            role=role.id,
            ok=True,
            summary=f"{role.label} completed '{item.task[:60]}' (simulated, no model call)",
            artifacts=artifacts,
            files_touched=list(item.owned_paths)[:3],
            evidence=[f"dry-run artefact for {name}" for name in role.outputs],
            steps_used=2,
            tokens_used=0,
            data={"simulated": True},
        )


class CallableExecutor:
    """Adapt a plain function into a :class:`WorkExecutor` (embedding, plugins)."""

    def __init__(self, func: Callable[[WorkItem, Role, WorkerContext], Any]) -> None:
        self.func = func

    #: Keys a mapping return value may use; anything else is kept in ``data``.
    KNOWN_KEYS = frozenset(
        {
            "ok",
            "summary",
            "artifacts",
            "files_touched",
            "evidence",
            "steps_used",
            "tokens_used",
            "error",
            "follow_ups",
            "duration_s",
        }
    )

    async def execute(self, item: WorkItem, role: Role, context: WorkerContext) -> WorkResult:
        """Adapt the function's return value into a :class:`WorkResult`.

        Three shapes are accepted: a ``WorkResult`` (passed through), a mapping
        (``ok``, ``summary``, ``artifacts``, ``files_touched``, ``evidence``,
        ``error``, ``follow_ups``, ``steps_used``, ``tokens_used`` — unknown keys
        land in ``data``), or any other value, which becomes the summary only.

        What is deliberately *not* done: inventing artefacts.  An earlier version
        filled every name in ``role.outputs`` with the stringified return value, so
        an adapter returning ``{"ok": True}`` "produced" a patch, tests and notes it
        never wrote — which defeated the required-outputs check and fed fabricated
        content into the merge.  An adapter must declare what it produced; if it
        cannot, the worker fails honestly.
        """

        outcome = self.func(item, role, context)
        if isinstance(outcome, Awaitable):
            outcome = await outcome
        if isinstance(outcome, WorkResult):
            return outcome
        if isinstance(outcome, Mapping):
            error = outcome.get("error")
            return WorkResult(
                item_id=item.id,
                role=role.id,
                ok=bool(outcome.get("ok", True)),
                summary=str(outcome.get("summary") or "")[:400],
                artifacts={str(key): str(value) for key, value in dict(outcome.get("artifacts") or {}).items()},
                files_touched=[str(path) for path in (outcome.get("files_touched") or [])],
                evidence=[str(entry) for entry in (outcome.get("evidence") or [])],
                steps_used=int(outcome.get("steps_used") or 1),
                tokens_used=int(outcome.get("tokens_used") or 0),
                error=str(error)[:400] if error else None,
                follow_ups=[str(entry) for entry in (outcome.get("follow_ups") or [])],
                data={key: value for key, value in outcome.items() if key not in self.KNOWN_KEYS},
            )
        text = str(outcome)[:400]
        return WorkResult(item_id=item.id, role=role.id, ok=bool(text), summary=text)
