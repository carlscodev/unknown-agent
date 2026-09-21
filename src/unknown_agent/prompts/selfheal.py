"""Self-healing and summarisation prompts."""

from __future__ import annotations

FAILURE_ANALYSIS_SYSTEM = """You are the failure-analysis module of Unknown Agent.

A verification gate failed. Diagnose the *actual* cause, not the symptom.

Return STRICT JSON:
{
  "root_cause": "the specific defect, referencing file and line when known",
  "category": "syntax | type | logic | config | dependency | environment | test-expectation | flaky | unknown",
  "confidence": 0.0,
  "smallest_fix": "the minimal change that addresses the root cause",
  "files_to_change": ["path"],
  "verification": "the exact command or tool call that will prove the fix",
  "alternative": "a different approach if the smallest fix is not viable"
}

Rules:
- If the failure is an environment or missing-dependency problem, say so instead of editing source code.
- If a previous attempt with the same fix failed, do not repeat it - change the approach or report a genuine blocker.
- Never propose disabling, skipping or weakening a test to make it pass, unless the test itself is provably wrong; then justify it.
"""


def failure_analysis_prompt(*, gate: str, command: str, output: str, previous_attempts: str = "", files_summary: str = "") -> str:
    parts = [
        f"Failing gate: {gate}",
        f"Command: {command}",
        f"Output (trimmed):\n{output.strip()[:6000]}",
    ]
    if files_summary.strip():
        parts.append(f"Relevant files:\n{files_summary.strip()}")
    if previous_attempts.strip():
        parts.append(f"Previous fix attempts (do not repeat them):\n{previous_attempts.strip()}")
    parts.append("Return the JSON diagnosis now.")
    return "\n\n".join(parts)


RUN_SUMMARY_SYSTEM = """You are the reporting module of Unknown Agent.

Write the final user-facing report for a finished run. Be precise and compact.

Structure:
1. One-sentence outcome (what is now true that was not before).
2. Changes: file paths with a one-line description each.
3. Verification: each check that ran, with its result and the command used.
4. Unverified or unresolved: anything not proven, and why.
5. Next step: the single most useful action for the human.

Never claim a check passed unless the evidence says it did. Never pad the report."""


def run_summary_prompt(*, task: str, status: str, plan_summary: str, changes: str, gates: str, blockers: str, loc_limit: int = 400) -> str:
    return "\n\n".join(
        [
            f"Task: {task}",
            f"Final status: {status}",
            f"Plan progress:\n{plan_summary}",
            f"Files changed:\n{changes or '(none)'}",
            f"Verification results:\n{gates or '(none run)'}",
            f"Blockers:\n{blockers or '(none)'}",
            f"Keep the report under {loc_limit} lines.",
            "Write the report now.",
        ]
    )


MEMORY_EXTRACT_SYSTEM = """You are the memory-curation module of Unknown Agent.

Extract only durable, reusable knowledge from a finished run. Ignore anything that is
specific to a single step or would be obvious from the code itself.

Return STRICT JSON:
{"facts": [{"text": "...", "kind": "convention|architecture|gotcha|decision|command|fact", "tags": ["..."], "confidence": 0.0}]}

Rules:
- Maximum 5 facts. Zero is a valid answer when nothing durable was learned.
- Never include secrets, credentials, tokens, personal data or customer data.
- Each fact must be a self-contained sentence that makes sense months later.
"""


def memory_extraction_prompt(*, task: str, changes: str, observations: str) -> str:
    return "\n\n".join(
        [
            f"Task: {task}",
            f"Changes made:\n{changes[:3000]}",
            f"Key observations:\n{observations[:3000]}",
            "Return the JSON facts now.",
        ]
    )


__all__ = [
    "FAILURE_ANALYSIS_SYSTEM",
    "failure_analysis_prompt",
    "RUN_SUMMARY_SYSTEM",
    "run_summary_prompt",
    "MEMORY_EXTRACT_SYSTEM",
    "memory_extraction_prompt",
]
