"""Planner: turn a task into an executable, verifiable plan.

The planner is deliberately strict:

* it asks the model for JSON against a documented schema and validates the result;
* it repairs common malformations (fenced JSON, trailing commentary, missing
  acceptance criteria) instead of failing the run;
* it falls back to a deterministic heuristic plan when the model is unavailable
  or returns garbage, so the product degrades instead of dying;
* it can revise a plan mid-run when reality contradicts it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from unknown_agent.context.builder import ContextBuilder
from unknown_agent.core.models import Message, Plan, PlanStep, StepStatus
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import ModelProvider, ModelRequest
from unknown_agent.providers.base import ToolSchema

log = get_logger("core.planner")

MAX_STEPS = 12

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _reasoning(settings: Any, surface: str, *, max_output_tokens: int | None = None) -> dict[str, Any]:
    """Reasoning wire params for a planning call (``[reasoning] planning_mode``).

    Planning is the surface where thinking pays for itself most, which is why the
    ladder has a separate rung for it — and why a plan request must not silently
    use the coding rung.
    """

    from unknown_agent.reasoning import params_for_settings

    params, _resolved = params_for_settings(settings, surface, max_output_tokens=max_output_tokens)
    return params


@dataclass(slots=True)
class PlannerResult:
    plan: Plan
    source: str  # "model" | "repaired" | "fallback"
    raw: str = ""
    warnings: list[str] = field(default_factory=list)


class Planner:
    """Produces and revises plans."""

    def __init__(self, provider: ModelProvider, *, builder: ContextBuilder, settings: Any) -> None:
        self.provider = provider
        self.builder = builder
        self.settings = settings

    async def plan(self, task: str, *, tool_schemas: list[ToolSchema], constraints: str = "") -> PlannerResult:
        specs = [
            {
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters.get("properties", {}),
                "required": schema.parameters.get("required", []),
                "risk": getattr(schema, "risk", "read"),
            }
            for schema in tool_schemas
        ]
        catalog_specs = [
            {**spec, "risk": "read"} for spec in specs
        ]
        messages, budget = self.builder.build_plan_messages(task=task, tool_specs=catalog_specs, constraints=constraints)
        request = ModelRequest(
            messages=messages,
            tools=[],
            temperature=min(self.settings.active_provider.temperature, 0.3),
            max_output_tokens=min(4000, self.settings.agent.max_tokens),
            json_mode=True,
            reasoning=_reasoning(self.settings, "plan", max_output_tokens=min(4000, self.settings.agent.max_tokens)),
        )
        response = await self.provider.complete(request)
        budget.conversation_tokens += response.usage.completion_tokens
        if not response.ok and not response.content:
            log.warning("planner provider error: %s", response.error)
            return PlannerResult(plan=self.fallback_plan(task, tool_schemas), source="fallback", warnings=[f"planner provider error: {response.error}"])

        warnings: list[str] = []
        payload = _extract_json(response.content)
        if payload is None:
            warnings.append("planner returned non-JSON output; using deterministic fallback plan")
            return PlannerResult(plan=self.fallback_plan(task, tool_schemas), source="fallback", raw=response.content, warnings=warnings)

        try:
            plan, repairs = _coerce_plan(payload, task, tool_schemas)
        except ValueError as exc:
            warnings.append(f"planner output could not be used ({exc}); using deterministic fallback plan")
            return PlannerResult(plan=self.fallback_plan(task, tool_schemas), source="fallback", raw=response.content, warnings=warnings)
        warnings.extend(repairs)
        source = "repaired" if repairs else "model"
        return PlannerResult(plan=plan, source=source, raw=response.content, warnings=warnings)

    async def revise(self, plan: Plan, *, failure: str, observations: str, tool_schemas: list[ToolSchema]) -> PlannerResult:
        from unknown_agent.prompts.planner import REVISION_SYSTEM, revision_user_prompt

        user = revision_user_prompt(
            task=plan.goal,
            plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
            failure=failure,
            observations=observations,
        )
        request = ModelRequest(
            messages=[_system_message(REVISION_SYSTEM), _user_message(user)],
            temperature=0.2,
            max_output_tokens=4000,
            json_mode=True,
            reasoning=_reasoning(self.settings, "plan", max_output_tokens=4000),
        )
        response = await self.provider.complete(request)
        payload = _extract_json(response.content) if response.ok else None
        if payload is None:
            return PlannerResult(
                plan=self.fallback_plan(plan.goal, tool_schemas, previous=plan),
                source="fallback",
                warnings=["revision failed; re-planned from scratch"],
            )
        try:
            revised, repairs = _coerce_plan(payload, plan.goal, tool_schemas)
        except ValueError as exc:
            return PlannerResult(
                plan=self.fallback_plan(plan.goal, tool_schemas, previous=plan),
                source="fallback",
                warnings=[f"revised plan was unusable ({exc}); re-planned from scratch"],
            )
        revised.revision = plan.revision + 1
        return PlannerResult(plan=revised, source="model", raw=response.content, warnings=repairs)

    # -- fallback ----------------------------------------------------------
    def fallback_plan(self, task: str, tool_schemas: list[ToolSchema], *, previous: Plan | None = None) -> Plan:
        """Deterministic, always-valid plan used when the model cannot plan."""

        names = {schema.name for schema in tool_schemas}
        steps: list[PlanStep] = []

        recon_tools = [name for name in ("inspect_project", "project_tree", "search_code", "read_file") if name in names]
        steps.append(
            PlanStep(
                title="Reconnaissance",
                description=f"Map the repository and locate the code that owns: {task}",
                tools=recon_tools,
                acceptance=["Relevant files identified", "Project commands discovered"],
                status=StepStatus.PENDING,
            )
        )
        steps.append(
            PlanStep(
                title="Implementation",
                description="Apply the smallest coherent change, matching existing conventions.",
                tools=[name for name in ("apply_patch", "write_file", "create_file") if name in names],
                acceptance=["Change is scoped to the task", "No unrelated files modified"],
            )
        )
        verify_tools = [name for name in ("run_gates", "test_run", "build_run", "lint_run", "browser_flow") if name in names]
        steps.append(
            PlanStep(
                title="Verification",
                description="Run the project's own checks and fix anything that fails.",
                tools=verify_tools,
                acceptance=["Required gates pass", "Evidence captured for each check"],
            )
        )
        if previous is not None:
            completed = [step for step in previous.steps if step.status is StepStatus.COMPLETED]
            return Plan(
                goal=previous.goal,
                steps=[*completed, *steps],
                deliverables=previous.deliverables or ["Working, verified change"],
                risks=previous.risks,
                verification=previous.verification or ["Project test suite", "Project build"],
                assumptions=previous.assumptions,
                revision=previous.revision + 1,
            )
        return Plan(
            goal=task,
            steps=steps,
            deliverables=["Scoped code change", "Passing verification gates", "Concise evidence-backed summary"],
            risks=["Hidden dependencies between files", "Incomplete discovery of conventions"],
            verification=["Discover and run the project's test command", "Run the build", "Run browser checks when the app has a UI"],
            assumptions=["Repository state is known via tools, not assumed"],
        )

    @staticmethod
    def summarise(plan: Plan) -> str:
        lines = [f"goal: {plan.goal}"]
        for index, step in enumerate(plan.steps, start=1):
            marker = {
                StepStatus.COMPLETED: "[x]",
                StepStatus.FAILED: "[!]",
                StepStatus.IN_PROGRESS: "[>]",
                StepStatus.BLOCKED: "[!]",
                StepStatus.SKIPPED: "[-]",
            }.get(step.status, "[ ]")
            lines.append(f"{marker} {index}. {step.title} - {step.description[:110]}")
        if plan.deliverables:
            lines.append("deliverables: " + "; ".join(plan.deliverables[:5]))
        return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of model output, tolerating fences and prose."""

    if not text:
        return None
    candidates: list[str] = []
    fenced = JSON_BLOCK_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _coerce_plan(payload: dict[str, Any], task: str, tool_schemas: list[ToolSchema]) -> tuple[Plan, list[str]]:
    """Validate and repair a model-provided plan."""

    repairs: list[str] = []
    available = {schema.name for schema in tool_schemas}
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("plan contains no steps")

    steps: list[PlanStep] = []
    for index, item in enumerate(raw_steps[:MAX_STEPS]):
        if not isinstance(item, dict):
            repairs.append(f"step {index + 1} was not an object; skipped")
            continue
        title = str(item.get("title") or item.get("name") or f"Step {index + 1}").strip()[:120]
        description = str(item.get("description") or item.get("details") or title).strip()
        tools = [str(tool) for tool in (item.get("tools") or []) if isinstance(tool, (str, int))]
        unknown_tools = [tool for tool in tools if tool not in available]
        if unknown_tools:
            repairs.append(f"step {index + 1} referenced unknown tools: {', '.join(unknown_tools)}")
            tools = [tool for tool in tools if tool in available]
        acceptance = [str(entry)[:200] for entry in (item.get("acceptance") or item.get("done") or []) if entry]
        if not acceptance:
            acceptance = [f"{title} is demonstrably complete"]
            repairs.append(f"step {index + 1} had no acceptance criteria; added a default")
        steps.append(PlanStep(title=title, description=description, tools=tools, acceptance=acceptance))

    if not steps:
        raise ValueError("plan had no usable steps")

    plan = Plan(
        goal=str(payload.get("goal") or task).strip()[:400],
        steps=steps,
        deliverables=[str(item)[:200] for item in (payload.get("deliverables") or []) if item],
        risks=[str(item)[:200] for item in (payload.get("risks") or []) if item],
        verification=[str(item)[:200] for item in (payload.get("verification") or []) if item],
        assumptions=[str(item)[:200] for item in (payload.get("assumptions") or []) if item],
    )
    if not plan.verification:
        plan.verification = ["Project test suite", "Project build"]
        repairs.append("plan had no verification section; added the default checks")
    return plan, repairs


def _system_message(content: str) -> Message:
    return Message.system(content)


def _user_message(content: str) -> Message:
    return Message.user(content)


__all__ = ["Planner", "PlannerResult"]
