"""Planner and step prompts.

The planner is asked for strict JSON so it can be parsed deterministically and
re-validated; a malformed plan is repaired, never trusted.  The schema lives in
the prompt because every provider - including text-only models - must support it.
"""

from __future__ import annotations

from collections.abc import Sequence

PLANNER_SYSTEM = """You are the planning module of Unknown Agent, a hyperagent that builds real software.

Produce an execution plan as STRICT JSON only - no prose, no markdown fences, no commentary.

JSON shape:
{
  "goal": "one sentence describing the concrete end state",
  "deliverables": ["observable artefacts, e.g. 'POST /checkout returns 201'"],
  "assumptions": ["what must be true; flag anything you would otherwise guess"],
  "risks": ["things that could break or are missing"],
  "verification": ["the exact checks that will prove completion"],
  "steps": [
    {
      "title": "short imperative title",
      "description": "what happens and where (files/areas), concrete enough to execute",
      "tools": ["tool names from the provided catalog"],
      "acceptance": ["objectively checkable condition for this step"]
    }
  ]
}

Rules:
- 3 to 8 steps. Each step must be independently verifiable.
- Order by dependency; never plan verification before implementation exists.
- Use only tools present in the catalog.
- Do not plan to "ask the user" unless the request is genuinely ambiguous.
- If the task is small (<= 3 files), produce a short plan rather than a ceremony.
- Never include steps that cannot be validated (e.g. "improve quality").
"""


def planner_user_prompt(
    *,
    task: str,
    project_summary: str,
    tool_catalog: str,
    memories: str = "",
    mode: str = "plan",
    constraints: str = "",
) -> str:
    parts = [
        f"Task:\n{task.strip()}",
        f"Project:\n{project_summary.strip() or '(no project detected)'}",
        f"Available tools:\n{tool_catalog.strip()}",
    ]
    if memories.strip():
        parts.append(f"Relevant memory:\n{memories.strip()}")
    if constraints.strip():
        parts.append(f"Constraints and policy:\n{constraints.strip()}")
    parts.append(f"Produce the JSON plan now ({mode} mode).")
    return "\n\n".join(parts)


REVISION_SYSTEM = """You are the planning module of Unknown Agent. The current plan is failing or stale.

Return STRICT JSON with the same shape as the original plan, but:
- keep steps that are still valid (preserve their wording so progress is not lost),
- mark the failed step as blocked and add a note explaining the evidence,
- add or replace only the steps needed to reach the goal,
- never plan the same failing action again without changing the approach.
"""


def revision_user_prompt(*, task: str, plan_json: str, failure: str, observations: str) -> str:
    return "\n\n".join(
        [
            f"Task:\n{task.strip()}",
            f"Current plan (JSON):\n{plan_json}",
            f"Why the plan is stale or failing:\n{failure.strip()}",
            f"Recent observations:\n{observations.strip()[:4000]}",
            "Return the revised plan JSON now.",
        ]
    )


STEP_SYSTEM = """You are the execution module of Unknown Agent, working one step of a plan inside a real repository.

Each turn: think briefly (2-4 sentences, concrete and technical), then either call tools or finish the step.

Rules:
- Prefer acting over describing. If you know the next tool call, make it.
- Match the repository's existing style, structure, naming and error handling.
- Never write placeholder implementations ("pass  # TODO", "your code here", mocks in production paths).
- If the step's acceptance criteria are already satisfied, say so and finish the step.
- If blocked, state exactly what blocks you and what the user must decide.
"""


def step_user_prompt(
    *,
    task: str = "",
    plan_summary: str,
    current_step: str,
    acceptance: Sequence[str],
    recent: str,
    extra: str = "",
) -> str:
    parts = [
        # The task comes first so every step is self-contained: a step prompt that
        # only shows the plan drifts as the plan grows.
        *([f"Task:\n{task.strip()}"] if task.strip() else []),
        f"Plan:\n{plan_summary.strip()}",
        f"Current step:\n{current_step.strip()}",
        "Acceptance criteria for this step:\n" + "\n".join(f"- {item}" for item in acceptance),
        f"Recent actions and observations:\n{recent.strip()[:6000] or '(nothing yet)'}",
    ]
    if extra:
        parts.append(extra.strip())
    return "\n\n".join(parts)


__all__ = [
    "PLANNER_SYSTEM",
    "planner_user_prompt",
    "REVISION_SYSTEM",
    "revision_user_prompt",
    "STEP_SYSTEM",
    "step_user_prompt",
]
