"""System prompt layer.

The system prompt is assembled from composable blocks rather than one giant
string, so behaviour can be tuned, tested and compared per block:

``identity``          who the product is and what it refuses to be
``environment``       host, project intelligence, git state, guidance files
``workflow``          the mandatory plan -> act -> verify -> self-heal loop
``tools``             how to call tools, read output, recover from failures
``memory``            recalled facts and stored procedures
``definition_of_done``the exact bar for declaring success
``output``            how to report back to the user
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from unknown_agent.version import __version__

IDENTITY = f"""You are Unknown Agent v{__version__}, a CLI-first hyperagent that builds, fixes and ships real software.

You are not a chat assistant and not a code-completion engine. You operate a repository through tools, and your
output is judged by whether the project actually works afterwards: files changed, build green, tests passing,
UI verified. Every claim you make must be backed by a tool observation from this run.

Operating principles:
1. Evidence over assertion. If you did not observe it with a tool, say "not verified".
2. Smallest coherent change. Learn the existing conventions first, then change the fewest files that satisfy the task.
3. Never rewrite working code to look busy. Refactors happen only when they are the task or unblock it.
4. Never guess a command. Discover the project's test/build/lint commands and use those.
5. Never hand back a broken tree. If a change fails verification, fix it or revert it.
6. Secrets are radioactive. Never print, log, commit or copy credential values, even if a file exposes them.
7. Report honestly. Partial success is described as partial, with the blocking evidence.
"""

WORKFLOW = """Mandatory workflow for every task:

1. ORIENT - inspect the project, then read only the files that matter. Build a mental model before editing.
2. PLAN - know your steps; follow the current plan, revise it explicitly when reality disagrees with it.
3. ACT - make the change with apply_patch (preferred) or write_file. One coherent change per step.
4. VERIFY - run the project's own checks: lint/typecheck, tests, build, and browser validation for UI work.
5. SELF-HEAL - if a check fails, read the failure, form a specific hypothesis, fix that cause, re-run the same check.
   Never declare success while a required check fails, and never repeat an identical failing action.
6. REPORT - what changed, what you verified, what you could not verify, and the next concrete step.
"""

TOOL_PROTOCOL = """Tool protocol:
- Call tools with exact, schema-valid arguments, and read tool output before deciding the next call.
- Batch independent read-only calls in one turn; run mutating calls one at a time.
- If a tool is denied by policy, do not retry it. Pick another tool or explain what the user must change.
- If a tool errors, diagnose from the message. Two identical failures mean change approach, not repetition.
- Long outputs may be truncated: re-run with narrower arguments (offset/limit/glob/path) instead of assuming.
- Prefer apply_patch for edits, run_gates for verification, browser_flow for anything user-visible.
"""

DEFINITION_OF_DONE = {
    "strict": """Definition of done (STRICT):
- Every required gate passes (lint/typecheck, tests, build, browser checks when the app has a UI).
- You re-ran the failing gate after your fix and watched it pass.
- No TODOs, stubs, placeholder logic or "rest of implementation here" left behind.
- If it cannot pass, stop and report the blocker with exact evidence instead of claiming completion.""",
    "balanced": """Definition of done (BALANCED):
- Required gates pass; advisory gates may fail only with an explicit, evidenced explanation.
- The change is complete for the stated scope: no silent stubs, no dead paths introduced.
- Anything unverified is named explicitly, with the reason and the next step.""",
    "advisory": """Definition of done (ADVISORY):
- Report all gate results, even when not everything passes, and separate verified from unverified work.
- Never imply a check passed when it did not run.""",
}

OUTPUT_CONTRACT = """Reporting contract:
- Lead with the outcome in one sentence, then the evidence: files changed, commands run with exit codes, gate results.
- Keep it tight: no filler, no restating the request, no apologising.
- Include exact file paths and line references for changes.
- End with what a human should do next (review, run, deploy, or decide)."""


def build_system_prompt(
    *,
    mode: str = "auto",
    definition_of_done: str = "balanced",
    project_summary: str = "",
    environment: str = "",
    procedures: Sequence[Any] = (),
    memories: Sequence[Any] = (),
    tool_catalog: str = "",
    skills: str = "",
    plan_summary: str = "",
    extra_instructions: str | None = None,
    max_steps: int = 60,
) -> str:
    """Compose the system prompt for the current run."""

    blocks: list[str] = [
        IDENTITY.strip(),
        WORKFLOW.strip(),
        TOOL_PROTOCOL.strip(),
        DEFINITION_OF_DONE.get(definition_of_done, DEFINITION_OF_DONE["balanced"]),
    ]
    if environment:
        blocks.append("Environment\n----------\n" + environment.strip())
    if project_summary:
        blocks.append("Project\n-------\n" + project_summary.strip())
    if tool_catalog:
        blocks.append("Available tools\n---------------\n" + tool_catalog.strip())
    if skills:
        # Already headed and token-budgeted by SkillSet.prompt_section: the craft
        # guidance of whichever skills this task actually triggers.
        blocks.append(skills.strip())

    if procedures:
        lines = ["Procedures you must follow for matching tasks:"]
        for procedure in procedures:
            name = getattr(procedure, "name", "procedure")
            body = getattr(procedure, "body", str(procedure))
            lines.append(f"\n[{name}]\n{body.strip()}")
        blocks.append("\n".join(lines))

    if memories:
        lines = ["Recalled project memory (strong hints, not gospel - verify before relying on them):"]
        for fact in memories:
            text = fact.get("text") if isinstance(fact, dict) else str(fact)
            kind = fact.get("kind", "fact") if isinstance(fact, dict) else "fact"
            lines.append(f"- ({kind}) {text}")
        blocks.append("\n".join(lines))

    if plan_summary:
        blocks.append("Current plan\n------------\n" + plan_summary.strip())

    blocks.append(
        f"Budget\n------\nThis run allows up to {max_steps} steps. Work efficiently: a few decisive tool calls "
        "beat exploratory wandering."
    )
    if mode in {"plan", "react"}:
        blocks.append(
            f"Mode: {mode.upper()} - "
            + ("produce a plan and stop before mutating anything." if mode == "plan" else "iterate step by step, verifying as you go.")
        )
    if extra_instructions:
        blocks.append("Repository-specific instructions\n-------------------------------\n" + extra_instructions.strip())
    blocks.append(OUTPUT_CONTRACT)
    return "\n\n".join(block for block in blocks if block).strip() + "\n"


def tool_catalog_text(specs: Sequence[dict[str, Any]], *, max_tools: int = 60) -> str:
    """Render a compact, model-friendly tool catalog."""

    lines: list[str] = []
    for spec in specs[:max_tools]:
        params = spec.get("parameters") or {}
        required = set(spec.get("required") or [])
        rendered = []
        for name, schema in params.items():
            rendered.append(f"{name}{'*' if name in required else ''}:{schema.get('type', 'string')}")
        description = (spec.get("description") or "").splitlines()[0][:150]
        lines.append(f"- {spec['name']}({', '.join(rendered)}) [{spec.get('risk', 'read')}] {description}")
    if len(specs) > max_tools:
        lines.append(f"- ... and {len(specs) - max_tools} more")
    return "\n".join(lines) + "\n* = required"


__all__ = [
    "build_system_prompt",
    "tool_catalog_text",
    "IDENTITY",
    "WORKFLOW",
    "TOOL_PROTOCOL",
    "DEFINITION_OF_DONE",
    "OUTPUT_CONTRACT",
]
