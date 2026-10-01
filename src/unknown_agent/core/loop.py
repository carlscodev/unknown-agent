"""The agent execution loop: plan -> act -> observe -> verify -> self-heal.

This is the module that makes Unknown Agent an agent instead of a completion
shell.  Its contract:

* **It runs until the definition of done is met or a real blocker exists.**
  Producing text is not completion; passing gates is.
* **Every step is journaled** (events, tool calls, results, token usage) so the
  run can be replayed, audited or resumed.
* **Failure becomes the next iteration.** A failing gate triggers a root-cause
  analysis, a targeted fix, and a re-run of the *same* check.
* **Repetition is detected and broken.** Two identical failing actions force a
  strategy change or a reported blocker - never an endless retry.
* **Budgets are enforced** (steps, tokens, wall clock) with a clear reason when
  the run stops early.
* **Verification is automatic.** After mutating work, required gates run without
  being asked for, because "declare done" must mean "checked".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.context.builder import ContextBudget, ContextBuilder
from unknown_agent.core.models import (
    AgentDecision,
    GateStatus,
    Message,
    Plan,
    PlanStep,
    RunBudget,
    RunState,
    RunStatus,
    StepRecord,
    StepStatus,
    ToolCall,
    ToolResult,
    VerificationReport,
)
from unknown_agent.core.planner import Planner
from unknown_agent.errors import BudgetExceeded, GateFailure, ProviderError, TaskBlocked
from unknown_agent.memory.working import WorkingMemory
from unknown_agent.observability.events import Event, EventBus
from unknown_agent.observability.logging import get_logger
from unknown_agent.prompts.planner import STEP_SYSTEM, step_user_prompt
from unknown_agent.providers.base import ModelProvider, ModelRequest, ModelResponse
from unknown_agent.tools.base import ToolContext
from unknown_agent.tools.executor import ToolExecutor
from unknown_agent.tools.registry import ToolRegistry

log = get_logger("core.loop")

ASYNC_HOOK = Callable[[Event], Awaitable[None] | None]


@dataclass(slots=True)
class LoopHooks:
    """Presentation/telemetry hooks so the core never imports the UI."""

    on_event: ASYNC_HOOK | None = None
    on_thought: Callable[[str], None] | None = None
    on_tool_start: Callable[[ToolCall], None] | None = None
    on_tool_end: Callable[[ToolCall, ToolResult], None] | None = None
    on_verification: Callable[[VerificationReport], None] | None = None
    on_message: Callable[[str], None] | None = None
    should_stop: Callable[[], bool] | None = None
    request_plan_approval: Callable[[Plan], bool] | None = None


@dataclass(slots=True)
class LoopOutcome:
    state: RunState
    steps: list[StepRecord] = field(default_factory=list)
    summary: str = ""
    blocked: bool = False
    blocker_reason: str = ""


class AgentLoop:
    """Stateful ReAct loop with plan tracking and verification gates."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        tool_context: ToolContext,
        context_builder: ContextBuilder,
        planner: Planner,
        validators: Any = None,
        working: WorkingMemory | None = None,
        events: EventBus | None = None,
        hooks: LoopHooks | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.registry = registry
        self.executor = executor
        self.tool_context = tool_context
        self.builder = context_builder
        self.planner = planner
        self.validators = validators
        self.working = working or context_builder.working
        self.events = events or tool_context.events
        self.hooks = hooks or LoopHooks()
        self.messages: list[Message] = []
        self.steps: list[StepRecord] = []
        self.budget = RunBudget(
            max_steps=settings.agent.max_steps,
            max_tokens=settings.agent.max_tokens,
            max_wall_clock_s=settings.agent.max_wall_clock_minutes * 60,
        )
        self.context_budget = ContextBudget(total_tokens=settings.agent.context_token_budget)
        self.self_heal_attempts = 0
        #: The last :class:`~unknown_agent.reasoning.ResolvedReasoning`, so the UI
        #: can show which rung was actually used and whether it was degraded.
        self.last_reasoning: Any = None
        self._consecutive_failures = 0
        self._verified_revision: str | None = None

    # ------------------------------------------------------------------ #
    # public entry point
    # ------------------------------------------------------------------ #
    async def run(self, state: RunState, *, plan: Plan | None = None) -> LoopOutcome:
        state.budget = self.budget
        self.working.start(state.task, constraints=[f"{key}: {value}" for key, value in _policy_constraints(self.settings).items()])
        self.emit("run.start", state.task, state_id=state.id, mode=state.mode, provider=state.provider, model=state.model)

        if plan is None:
            plan = await self._create_plan(state)
        state.plan = plan
        self.emit("plan.created", f"plan with {len(plan.steps)} step(s)", source="model", steps=[step.title for step in plan.steps])
        if self.hooks.request_plan_approval is not None and not self.hooks.request_plan_approval(plan):
            state.finish(RunStatus.INTERRUPTED, summary="Plan rejected by the user.")
            self.emit("run.end", "plan rejected", status=state.status.value)
            return LoopOutcome(state=state, steps=self.steps, summary=state.summary)

        self.messages = [
            self.builder.build_system_message(
                tool_specs=[tool.spec() for tool in self.registry.all()],
                task=state.task,
                mode=state.mode,
                plan_summary=Planner.summarise(plan),
            ),
            Message.user(self._task_message(state, plan)),
        ]

        while True:
            if self._should_stop():
                state.finish(RunStatus.INTERRUPTED, summary="Interrupted by the user.")
                self.emit("interrupt", "run interrupted", status=state.status.value)
                break
            if self.budget.exhausted:
                reason = self.budget.reason() or "budget exhausted"
                state.blockers.append(reason)
                state.finish(RunStatus.BLOCKED, summary=f"Stopped: {reason}.")
                self.emit("budget.warning", reason, budget=self.budget.to_dict())
                break

            step = plan.current_step
            if step is None:
                # Every step is complete: verify, self-heal, then finish.
                await self._finalise(state)
                break

            if step.status is StepStatus.PENDING:
                step.status = StepStatus.IN_PROGRESS
                self.emit("step.start", f"step {self.budget.steps_used + 1}: {step.title}", step_title=step.title)

            continuation = await self._run_step(state, plan, step)
            if not continuation:
                break

        if state.finished_at is None:
            state.finish(RunStatus.COMPLETED if not state.blockers else RunStatus.BLOCKED, summary=state.summary or "Run finished.")
        self._persist_summary(state)
        self.emit("run.end", state.summary or "run finished", status=state.status.value, steps=self.budget.steps_used, tokens=self.budget.tokens_used)
        return LoopOutcome(
            state=state,
            steps=self.steps,
            summary=state.summary,
            blocked=state.status in {RunStatus.BLOCKED, RunStatus.FAILED},
            blocker_reason="; ".join(state.blockers),
        )

    # ------------------------------------------------------------------ #
    # one step of the loop
    # ------------------------------------------------------------------ #
    async def _run_step(self, state: RunState, plan: Plan, step: PlanStep) -> bool:
        """Execute one ReAct step.  Returns False when the run must stop."""

        self.budget.steps_used += 1
        state.step = self.budget.steps_used
        self.tool_context.step = self.budget.steps_used
        record = StepRecord(index=self.budget.steps_used, decision=AgentDecision())
        started = time.perf_counter()

        messages = self.builder.fit_messages(
            [
                *self.messages,
                Message.user(
                    step_user_prompt(
                        task=state.task,
                        plan_summary=Planner.summarise(plan),
                        current_step=f"{step.title}\n{step.description}",
                        acceptance=step.acceptance,
                        recent=self.working.recent(count=8),
                        extra="Verification note: " + self._verification_note(state),
                    )
                ),
            ],
            budget=self.context_budget,
        )

        response = await self._model_turn(messages, state)
        if response is None:
            step.status = StepStatus.BLOCKED
            return False

        decision = _decision_from_response(response)
        record.decision = decision
        record.prompt_tokens = response.usage.prompt_tokens
        record.completion_tokens = response.usage.completion_tokens
        self.budget.tokens_used += response.usage.total_tokens

        if decision.thought:
            self.working.learn(decision.thought.strip()[:300])
            if self.hooks.on_thought:
                self.hooks.on_thought(decision.thought)
            self.emit("thought", decision.thought[:1500])

        self.messages.append(Message.assistant(decision.answer or decision.thought, tool_calls=decision.tool_calls))

        # No tool calls: the model is talking. Treat it as an observation turn.
        if not decision.tool_calls:
            record.finished_at = time.time()
            self.steps.append(record)
            self.working.record_observation("assistant", decision.answer or decision.thought or "(no content)", ok=True, step=record.index)
            if decision.final:
                return await self._handle_final(state, plan, step, decision)
            return True

        results = await self._execute(state, decision.tool_calls)
        record.results = results
        record.observations = [result.render() for result in results]
        record.finished_at = time.time()
        self.steps.append(record)

        for call, result in zip(decision.tool_calls, results, strict=False):
            self.messages.append(Message.tool_result(call.id, result.render(), name=call.name))
            summary = (result.output if result.ok else (result.error or result.output) or "").splitlines()[:3]
            self.working.record_observation(call.name, " ".join(summary)[:300] or "(no output)", ok=result.ok, step=record.index)

        failures = [result for result in results if not result.ok]
        if failures:
            self._consecutive_failures += 1
            signature = " ".join((result.error or "")[:80] for result in failures)
            if self.working.repeated_failure(signature, threshold=2):
                self.working.add_hypothesis("Repeated identical failure detected - change approach instead of retrying.")
                self.messages.append(
                    Message.user(
                        "The same tool failure has now happened twice. Do not repeat it. "
                        "Change approach: different tool, narrower arguments, or report a blocker with evidence."
                    )
                )
            if self._consecutive_failures >= self.settings.agent.max_consecutive_failures:
                step.status = StepStatus.BLOCKED
                state.blockers.append(f"{self._consecutive_failures} consecutive tool failures at step {record.index}")
                state.finish(RunStatus.BLOCKED, summary="Stopped after repeated tool failures; see the journal for evidence.")
                return False
        else:
            self._consecutive_failures = 0

        if decision.final:
            return await self._handle_final(state, plan, step, decision)

        # Periodically check whether the step's acceptance criteria are already met.
        if record.index % max(1, self.settings.agent.reflection_every_steps) == 0:
            self.messages.append(
                Message.user(
                    "Reflection checkpoint: state in one sentence whether the current step's acceptance criteria are met. "
                    "If they are, finish the step and move on to the next plan item."
                )
            )
        return True

    # ------------------------------------------------------------------ #
    # model interaction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _surface(state: RunState) -> str:
        """Which reasoning rung this turn belongs to.

        The run's own mode/kind is the honest signal: a ``plan`` run plans, a
        ``fix`` run codes, an ``ask`` run chats.  Falling back to ``run`` keeps the
        default rung for anything the ladder does not name.
        """

        for value in (getattr(state, "mode", ""), getattr(state, "kind", "")):
            candidate = str(value or "").strip().lower()
            if candidate and candidate != "auto":
                return candidate
        return "run"


    def reasoning_params(self, surface: str, *, state: RunState | None = None) -> dict[str, Any]:
        """Vendor-neutral reasoning parameters for one request.

        ``[reasoning]`` is a *product* ladder (medium … ultra); this is the single
        place it becomes wire parameters.  :func:`unknown_agent.reasoning.resolve`
        negotiates the requested depth down to what the vendor can honour, so:

        * a model with native control gets ``reasoning_effort`` /
          ``thinking.budget_tokens`` / ``reasoning_tokens`` as appropriate;
        * a model without one gets prompt scaffolding and a larger output budget
          when ``fallback = "scaffold"``, or nothing at all when ``ignore``;
        * either way an empty dict is a valid answer meaning "this vendor has
          nothing to honour" — degradation is graceful, never an error.
        """

        from unknown_agent.reasoning import params_for_settings

        provider = self.settings.active_provider
        params, resolved = params_for_settings(
            self.settings,
            surface,
            escalate_by=self.self_heal_attempts,
            max_output_tokens=int(provider.max_output_tokens or 8_000),
            temperature=provider.temperature,
        )
        if resolved is not None:
            self.last_reasoning = resolved
        return params

    async def _model_turn(self, messages: list[Message], state: RunState) -> ModelResponse | None:
        request = ModelRequest(
            messages=messages,
            tools=self.registry.schemas(),
            temperature=self.settings.active_provider.temperature,
            max_output_tokens=self.settings.active_provider.max_output_tokens,
            reasoning=self.reasoning_params(self._surface(state), state=state),
        )
        attempts = 0
        while attempts < 3:
            attempts += 1
            started = time.perf_counter()
            self.emit("provider.request", f"{state.model} request ({len(messages)} messages)", attempt=attempts)
            try:
                response = await self.provider.complete(request)
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport layers vary per provider
                log.warning("provider call failed (%s); retrying", exc)
                await asyncio.sleep(min(8.0, 1.5 * attempts))
                continue
            self.emit(
                "provider.response",
                f"{state.model} responded in {response.latency_ms / 1000:.1f}s"
                + (f" [error: {response.error}]" if response.error else ""),
                duration_ms=response.latency_ms,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                tool_calls=len(response.tool_calls),
            )
            if response.ok or response.content or response.tool_calls:
                return response
            log.warning("provider returned an error: %s", response.error)
            if attempts >= 3:
                # Provider failures are normalised before they reach the user: the raw
                # HTTP body is evidence, not an explanation.
                from unknown_agent.providers.toolkit import normalise_provider_error

                friendly = normalise_provider_error(
                    Exception(response.error or "provider failed"),
                    provider=getattr(self.provider, "vendor", getattr(self.provider, "name", "provider")),
                    model=getattr(self.provider, "model", ""),
                )
                state.blockers.append(friendly.message)
                if friendly.hint:
                    self.emit("log", friendly.hint, level="warning")
                state.finish(RunStatus.BLOCKED, summary=f"Model provider failed: {response.error}")
                return None
            await asyncio.sleep(min(8.0, 1.5 * attempts))
        return None

    async def _execute(self, state: RunState, calls: list[ToolCall]) -> list[ToolResult]:
        batch = calls[: self.settings.agent.max_tool_calls_per_step]
        if len(calls) > len(batch):
            self.emit("budget.warning", f"{len(calls) - len(batch)} tool call(s) skipped (max_tool_calls_per_step)")
        self.budget.tool_calls += len(batch)
        if self.hooks.on_tool_start:
            for call in batch:
                self.hooks.on_tool_start(call)
        results = await self.executor.execute_many(batch)
        for call, result in zip(batch, results, strict=False):
            if self.hooks.on_tool_end:
                self.hooks.on_tool_end(call, result)
            self._track_effects(state, call, result)
        return results

    def _track_effects(self, state: RunState, call: ToolCall, result: ToolResult) -> None:
        if not result.ok:
            return
        path = result.data.get("path") or call.arguments.get("path")
        if call.name in {"write_file", "apply_patch", "create_file", "delete_path", "move_path"} and path:
            if path not in state.files_changed:
                state.files_changed.append(str(path))
            self.working.note_file(str(path))
        command = result.data.get("command") or call.arguments.get("command")
        if command:
            state.commands_run.append(str(command))
            self.working.note_command(str(command))
        for artifact in result.artifacts:
            if artifact and artifact not in state.artifacts:
                state.artifacts.append(artifact)

    # ------------------------------------------------------------------ #
    # completion and verification
    # ------------------------------------------------------------------ #
    async def _handle_final(self, state: RunState, plan: Plan, step: PlanStep, decision: AgentDecision) -> bool:
        """The model claims the step is done.  Verify before believing it."""

        step.status = StepStatus.COMPLETED
        step.notes = (decision.answer or decision.thought)[:1000]
        self.emit("step.end", f"step complete: {step.title}", step_title=step.title, status="completed")

        is_last_step = bool(plan.steps) and step is plan.steps[-1]
        if _is_verification_step(step) or is_last_step or plan.completed:
            passed = await self._verify(state, reason=f"after step '{step.title}'")
            if not passed:
                return await self._self_heal(state, plan)
        return True

    async def _verify(self, state: RunState, *, reason: str, gates: list[str] | None = None) -> bool:
        if self.validators is None:
            return True
        mutating_tools = [record for record in self.executor.records if record.risk in {"write", "destructive"}]
        strict = self.settings.agent.definition_of_done == "strict"
        if not mutating_tools and gates is None and not strict:
            # Nothing was written, so most gates would only re-measure the status quo.
            # `--dod strict` deliberately opts out: it wants the evidence either way.
            self.emit("gate.started", "verification skipped: no changes were made", required=False)
            return True
        self.emit("gate.started", f"running quality gates ({reason})", update=True)
        report: VerificationReport = await self.validators.run(gates=gates, base_url=self.settings.browser.base_url)
        state.verification = report
        self.emit(
            "gate.finished",
            f"gates: {len([item for item in report.results if item.passed])}/{len(report.results)} passed",
            passed=report.passed,
            results=[item.to_dict() for item in report.results],
        )
        if self.hooks.on_verification:
            self.hooks.on_verification(report)
        if report.passed:
            self.working.learn("quality gates pass")
            return True
        for failure in report.failures:
            self.working.record_observation("gate", f"{failure.name} failed: {failure.summary[:200]}", ok=False, step=self.budget.steps_used)
        return False

    async def _self_heal(self, state: RunState, plan: Plan) -> bool:
        """Turn a failing gate into a targeted repair attempt."""

        report = state.verification
        if report is None or not report.failures:
            return True
        if self.self_heal_attempts >= self.settings.agent.max_self_heal_attempts:
            state.blockers.append(
                f"{len(report.failures)} gate(s) still failing after {self.self_heal_attempts} repair attempt(s): "
                + ", ".join(item.name for item in report.failures)
            )
            state.finish(RunStatus.BLOCKED, summary="Verification still failing; stopping with evidence instead of claiming success.")
            self.emit("run.blocked", state.blockers[-1])
            return False

        self.self_heal_attempts += 1
        failure = report.failures[0]
        self.emit(
            "observation",
            f"self-heal attempt {self.self_heal_attempts}: {failure.name}",
            gate=failure.name,
            exit_code=failure.exit_code,
        )
        diagnosis = await self._diagnose(failure)
        heal_step = PlanStep(
            title=f"Fix {failure.name} failure (attempt {self.self_heal_attempts})",
            description=diagnosis,
            tools=["read_file", "apply_patch", "search_code"],
            acceptance=[f"{failure.name} passes on re-run: {failure.command or failure.name}"],
        )
        # Insert the repair step directly before the remaining verification steps.
        insert_at = max(0, len(plan.steps) - 1)
        plan.steps.insert(insert_at, heal_step)
        self.emit("plan.revised", f"inserted repair step for {failure.name}", revision=plan.revision + 1)
        self.working.add_hypothesis(diagnosis[:200])
        return True

    async def _diagnose(self, failure: Any) -> str:
        from unknown_agent.prompts.selfheal import FAILURE_ANALYSIS_SYSTEM, failure_analysis_prompt

        prompt = failure_analysis_prompt(
            gate=failure.name,
            command=failure.command or failure.name,
            output=failure.output or failure.summary,
            previous_attempts="\n".join(f"- attempt {index + 1}: {result.summary}" for index, result in enumerate(self.steps[-3:])),
        )
        request = ModelRequest(
            messages=[
                Message.system(FAILURE_ANALYSIS_SYSTEM),
                Message.user(prompt),
            ],
            temperature=0.1,
            max_output_tokens=1200,
            json_mode=True,
            reasoning=self.reasoning_params("heal"),
        )
        response = await self.provider.complete(request)
        self.budget.tokens_used += response.usage.total_tokens
        self.emit("provider.response", "failure analysis complete", prompt_tokens=response.usage.prompt_tokens)
        if not response.ok or not response.content:
            return (
                f"Read the failing output, fix the root cause, then re-run: {failure.command or failure.name}. "
                f"Output tail:\n{(failure.output or '')[-1200:]}"
            )
        payload = _extract_json(response.content)
        if payload:
            return (
                f"{payload.get('root_cause', 'cause unclear')}\n"
                f"Smallest fix: {payload.get('smallest_fix', 'see diagnosis')}\n"
                f"Verify with: {payload.get('verification', failure.command or failure.name)}"
            )
        return response.content[:1500]

    async def _finalise(self, state: RunState) -> bool:
        """All plan steps are complete.  Verify, self-heal, or finish."""

        passed = await self._verify(state, reason="final verification")
        if not passed:
            return await self._self_heal(state, state.plan or Plan(goal=state.task, steps=[]))
        report = state.verification
        if report is None:
            verified = "gates skipped (no files changed)"
        else:
            verified = "gates: " + ", ".join(f"{item.name}={item.status.value}" for item in report.results)
        state.finish(RunStatus.COMPLETED, summary=f"Task complete: {state.task[:160]}. Verification: {verified}.")
        # `run.end` is emitted exactly once, by run(); this event is informational.
        self.emit("run.finalised", state.summary, status="completed", verification=report.to_dict() if report else None)
        return False

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    async def _create_plan(self, state: RunState) -> Plan:
        result = await self.planner.plan(state.task, tool_schemas=self.registry.schemas())
        for warning in result.warnings:
            self.emit("log", warning, level="warning")
        state.plan = result.plan
        return result.plan

    def _task_message(self, state: RunState, plan: Plan) -> str:
        deliverables = "\n".join(f"- {item}" for item in plan.deliverables) or "- as described in the plan"
        lines = [
            f"Task: {state.task}",
            "",
            "Deliverables:",
            deliverables,
        ]
        if plan.verification:
            lines += ["", "Verification that will define done:", *[f"- {item}" for item in plan.verification]]
        if plan.assumptions:
            lines += ["", "Assumptions to validate early:", *[f"- {item}" for item in plan.assumptions]]
        lines += [
            "",
            "Begin with the first plan step. Verify work with the project's own commands before finishing a step.",
        ]
        return "\n".join(lines)

    def _verification_note(self, state: RunState) -> str:
        if state.verification is None:
            return "no gates have run yet this session"
        report = state.verification
        failing = ", ".join(item.name for item in report.failures) or "none"
        return f"last gate run passed={report.passed}; failing: {failing}"

    def _persist_summary(self, state: RunState) -> None:
        """Attach final statistics to the journal (the run.end event follows)."""

        self.emit(
            "log",
            "run statistics",
            stats=self.executor.stats(),
            files_changed=state.files_changed,
            budget=self.budget.to_dict(),
        )

    def _should_stop(self) -> bool:
        return bool(self.hooks.should_stop and self.hooks.should_stop())

    def emit(self, event_type: str, message: str = "", **data: Any) -> None:
        event = Event(type=event_type, run_id=self.tool_context.run_id, step=self.budget.steps_used, message=message, data=data)  # type: ignore[arg-type]
        self.events.emit(event)
        if self.hooks.on_event:
            try:
                result = self.hooks.on_event(event)
                if asyncio.iscoroutine(result):
                    with contextlib.suppress(RuntimeError):
                        asyncio.get_running_loop().create_task(result)
            except Exception:  # pragma: no cover - a bad hook must not kill the run
                log.debug("event hook failed", exc_info=True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _decision_from_response(response: ModelResponse) -> AgentDecision:
    content = (response.content or "").strip()
    thought, answer = _split_thought(content)
    final = not response.tool_calls and response.finish_reason in {"stop", "length"} and bool(content)
    return AgentDecision(thought=thought, answer=answer, tool_calls=list(response.tool_calls), final=final)


def _split_thought(content: str) -> tuple[str, str]:
    """Split an assistant message into reasoning and the user-facing statement."""

    if not content:
        return "", ""
    markers = ("FINAL:", "Final:", "SUMMARY:", "Summary:", "DONE:", "Done:")
    for marker in markers:
        if marker in content:
            head, _, tail = content.partition(marker)
            return head.strip(), tail.strip()
    lines = content.splitlines()
    if len(lines) <= 4:
        return content, content
    return "\n".join(lines[:3]), content


def _is_verification_step(step: PlanStep) -> bool:
    text = f"{step.title} {step.description}".lower()
    return any(keyword in text for keyword in ("verify", "verification", "test", "validate", "check", "qa", "build"))


def _policy_constraints(settings: Settings) -> dict[str, Any]:
    policy = settings.policy
    return {
        "read_only": policy.read_only,
        "network": policy.allow_network,
        "outside_workspace": policy.allow_outside_workspace,
        "destructive_allowed": policy.allow_destructive,
        "dry_run": policy.dry_run,
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    from unknown_agent.core.planner import _extract_json as extract

    return extract(text)


__all__ = ["AgentLoop", "LoopHooks", "LoopOutcome"]
