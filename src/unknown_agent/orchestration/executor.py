"""The production executor: a worker *is* a bounded agent run.

:mod:`~unknown_agent.orchestration.worker` defines the seam
(:class:`~unknown_agent.orchestration.worker.WorkExecutor`) and ships two
implementations that never call a model — ``OfflineExecutor`` for deterministic
tests and ``CallableExecutor`` for embedders.  This module is the third one, the
one the product uses: it turns a :class:`WorkItem` into a
:class:`~unknown_agent.core.runner.RunRequest` and executes it through
:func:`~unknown_agent.core.runner.run_task`, so every worker gets the real loop,
the real tools, the real gates and the real audit trail — bounded by its role.

What the role controls
----------------------
``tools``         the role's grant becomes the run's allow-list; the runner
                  *withholds* every other tool with a reason, so the boundary is
                  enforced rather than requested.
``read_only``     a role that does not mutate gets a read-only run, so the policy
                  layer refuses writes even if the model tries one.
``max_steps``     the smaller of the role's and the item's limit.
``verify_gates``  the gates this worker must satisfy, passed straight through.
``reasoning_mode``carried in the run metadata for the reasoning resolver.
``deny_tools``    restated in the instructions *and* enforced by policy.

Honest artefacts
----------------
A worker's declared outputs (``coder`` → ``patch``, ``tests``, ``notes``) are
checked by :meth:`Worker.run`: a required role that produced none of them fails.
So this executor must not invent them.  Each output is resolved from, in order:

* **evidence-derived outputs** (``patch``, ``tests``, ``screenshots``,
  ``gate_report``, …) come *only* from the run's diff, changed files and gate
  reports.  A claim is not evidence: ``patch: implemented the cart`` over an empty
  diff leaves the output missing, and the required-output check fails the worker.
* **prose outputs** (``plan``, ``risks``, ``repo_map``, ``readme``, …) come from
  the ``<deliverables>`` block the prompt asks for, because that content exists
  nowhere else; evidence supplements it where it applies.
* **anything unresolved stays missing** — a missing deliverable is a failure, not
  a fabrication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from unknown_agent.core.loop import LoopHooks
from unknown_agent.core.models import GateStatus
from unknown_agent.core.runner import RunRequest, RunResult, run_task
from unknown_agent.observability.logging import get_logger
from unknown_agent.orchestration.roles import Role
from unknown_agent.orchestration.worker import WorkExecutor, WorkItem, WorkResult, Worker, WorkerContext

log = get_logger("orchestration.executor")

__all__ = ["AgentExecutor", "parse_deliverables", "deliverables_instruction"]

#: The block the worker is asked to end its final message with.
_DELIVERABLES_RE = re.compile(r"<deliverables>(.*?)</deliverables>", re.IGNORECASE | re.DOTALL)

#: Outputs that are prose the model writes, so its own declaration is the primary
#: source.  Everything else is resolved from evidence first.
_PROSE_OUTPUTS = frozenset(
    {
        "plan",
        "blueprint",
        "alternatives",
        "risks",
        "notes",
        "conventions",
        "commands",
        "repo_map",
        "recommendation",
        "diagnostics",
        "provider_state",
        "rollback_plan",
        "deploy_report",
        "decision_records",
        "runbook",
        "readme",
        "a11y_notes",
    }
)


def deliverables_instruction(outputs: tuple[str, ...] | list[str]) -> str:
    """The prompt fragment that asks a worker to declare what it produced.

    Naming the outputs explicitly is what makes the merge meaningful: without it
    the conductor only learns "the run finished", not which deliverable arrived.
    """

    if not outputs:
        return ""
    lines = "\n".join(f"{name}: <one line: what you produced, or 'none' if you did not>" for name in outputs)
    return (
        "End your final message with a deliverables block, one line per item, so the "
        "conductor can merge your work without guessing:\n"
        f"<deliverables>\n{lines}\n</deliverables>\n"
        "Report only what you actually produced. 'none' is a useful answer; a claimed "
        "deliverable that does not exist is not."
    )


def parse_deliverables(text: str) -> dict[str, str]:
    """Extract the ``<deliverables>`` block, ignoring anything unusable."""

    if not text:
        return {}
    match = _DELIVERABLES_RE.search(text)
    if not match:
        return {}
    declared: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower().replace(" ", "_")
        value = value.strip()
        if not name or not value or value.lower() in {"none", "n/a", "na", "-"}:
            continue
        declared[name] = value
    return declared


@dataclass
class AgentExecutor:
    """Execute a work item as a real, bounded agent run.

    ``loaded`` is the same :class:`~unknown_agent.config.loader.LoadedSettings`
    the CLI builds, so a worker inherits the user's provider, policy, memory and
    tool configuration.  Per-run overrides exist because a conductor may want a
    cheaper model for inspection work and a stronger one for code generation.
    """

    loaded: Any
    #: Model/provider overrides applied to every worker run.
    model: str | None = None
    provider: str | None = None
    #: Simulate writes instead of applying them (``ua orchestrate run --dry-run``).
    dry_run: bool = False
    #: A worker cannot stop and ask a human mid-graph, so confirmations are
    #: pre-answered.  The role's ``read_only`` boundary still applies.
    yes: bool = True
    non_interactive: bool = True
    #: Extra gate names to require on top of the role's own.
    extra_gates: tuple[str, ...] = ()
    #: Called with the finished run, for live UI progress.
    on_run: Callable[[WorkItem, RunResult], None] | None = None
    #: Replace the runner (tests, embedders).  Signature matches ``run_task``.
    runner: Callable[..., Any] | None = None
    #: Fill ``WorkerContext.skill_prompt`` from the loaded settings when the caller
    #: left it empty, so a delegated worker gets the same craft guidance a solo run
    #: would.  Resolved once and cached: it reads the disk.
    use_skills: bool = True
    _runs: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _skills: Any = field(default=None, repr=False)

    # -- skills -------------------------------------------------------------- #
    def skill_set(self, context: WorkerContext) -> Any:
        """The resolved skills for this project, or ``None`` (cached per executor)."""

        if not self.use_skills or self.loaded is None:
            return None
        if self._skills is None:
            settings = getattr(self.loaded, "settings", None)
            if settings is None:
                return None
            try:
                from unknown_agent.skills import resolve_skills

                root = Path(context.project_root or ".")
                # Same rule as Runner._resolve_skills: without ``user_dir`` the
                # user-scope directory resolves to None and ~/.config/unknown-agent/
                # skills is invisible, so a worker would get less craft guidance
                # than the solo run that delegated it.
                paths = getattr(self.loaded, "paths", None)
                user_dir = getattr(paths, "user_skills_dir", None)
                self._skills = resolve_skills(settings, user_dir=user_dir, project_dir=root) or False
            except Exception as exc:  # noqa: BLE001 - skills must never stop a run
                log.debug("worker skills unavailable: %s", exc)
                self._skills = False
        return self._skills or None

    def skill_prompt(self, item: WorkItem, context: WorkerContext) -> str:
        """Task-relevant craft guidance for this worker, if any."""

        if context.skill_prompt:
            return context.skill_prompt
        resolved = self.skill_set(context)
        if resolved is None:
            return ""
        try:
            return resolved.prompt_section(task=f"{item.task} {context.task}")
        except Exception:  # noqa: BLE001
            return ""

    # -- request construction --------------------------------------------- #
    def build_request(self, item: WorkItem, role: Role, context: WorkerContext) -> RunRequest:
        """Turn a work item into a bounded run request."""

        context = WorkerContext(
            project_root=context.project_root,
            task=context.task or item.task,
            blueprint_text=context.blueprint_text,
            repo_map=context.repo_map,
            conventions=list(context.conventions),
            skill_prompt=context.skill_prompt or self.skill_prompt(item, context),
            policy_summary=context.policy_summary,
            previous_results=dict(context.previous_results),
            settings=context.settings,
        )
        prompt = Worker(role, context=context).build_prompt(item)
        instruction = deliverables_instruction(item.outputs or list(role.outputs))
        extra = "\n\n".join(part for part in (self._context_brief(context, item), instruction) if part)
        gates = list(dict.fromkeys([*role.verify_gates, *self.extra_gates]))
        steps = min(int(role.max_steps or 24), int(item.max_steps or role.max_steps or 24))
        return RunRequest(
            task=prompt,
            mode="auto",
            kind="run",
            read_only=not role.mutates,
            dry_run=self.dry_run,
            yes=self.yes,
            non_interactive=self.non_interactive,
            model=self.model,
            provider=self.provider,
            max_steps=steps,
            gates=gates or None,
            # The role's grant becomes a hard boundary: the runner withholds every
            # other tool with this reason, so a documenter cannot call shell_run and
            # an inspector cannot write.  Advisory boundaries are not boundaries.
            tools=list(role.tools) or None,
            # A worker is a *leaf* of the decomposition.  Without this the nested
            # Runner re-runs the delegation decision, sees the same complex task and
            # spawns its own conductor — recursion that multiplies workers per level
            # until the process runs out of file descriptors.  The conductor already
            # chose the shape; a worker's job is to do the work.
            orchestrate=False,
            extra_instructions=extra or None,
            metadata={
                "orchestration": {
                    "work_id": item.id,
                    "role": role.id,
                    "reasoning_mode": item.reasoning_mode or role.reasoning_mode,
                    "owned_paths": list(item.owned_paths),
                    "deny_tools": list(role.deny_tools),
                    "parallel_group": item.parallel_group,
                    "attempt": item.attempts,
                }
            },
        )

    def _context_brief(self, context: WorkerContext, item: WorkItem) -> str:
        """Everything the worker knows that is not in its own task line."""

        parts: list[str] = []
        if context.repo_map:
            parts.append(f"REPOSITORY MAP\n{context.repo_map[:2000]}")
        if context.conventions:
            parts.append("CONVENTIONS\n" + "\n".join(f"- {entry}" for entry in context.conventions[:12]))
        if context.policy_summary:
            parts.append(f"POLICY\n{context.policy_summary[:600]}")
        if context.skill_prompt:
            parts.append(context.skill_prompt[:1200])
        if context.previous_results:
            lines = [
                f"- [{result.role}] {result.summary[:160]}" for result in context.previous_results.values()
            ]
            parts.append("ALREADY DONE BY OTHER WORKERS\n" + "\n".join(lines[:8]))
        if item.context:
            parts.append(f"ADDITIONAL CONTEXT\n{item.context[:1200]}")
        if item.acceptance:
            parts.append("ACCEPTANCE CRITERIA\n" + "\n".join(f"- {entry}" for entry in item.acceptance[:8]))
        return "\n\n".join(parts)

    # -- execution --------------------------------------------------------- #
    async def execute(self, item: WorkItem, role: Role, context: WorkerContext) -> WorkResult:
        request = self.build_request(item, role, context)
        hooks = LoopHooks()
        runner = self.runner or run_task
        try:
            result = await runner(self.loaded, request, hooks=hooks)
        except Exception as exc:  # noqa: BLE001 - a worker must never kill the conductor
            log.warning("worker %s run raised %s: %s", item.id, type(exc).__name__, exc)
            return WorkResult(
                item_id=item.id,
                role=role.id,
                ok=False,
                summary=f"{role.label} could not run",
                error=f"{type(exc).__name__}: {exc}",
            )
        self._runs.append({"work_id": item.id, "role": role.id, "run_id": result.run_id})
        if self.on_run is not None:
            try:
                self.on_run(item, result)
            except Exception:  # pragma: no cover - observers must not break a run
                log.debug("orchestration on_run observer failed for %s", item.id)
        return self.to_work_result(item, role, result)

    # -- result mapping ---------------------------------------------------- #
    def to_work_result(self, item: WorkItem, role: Role, result: RunResult) -> WorkResult:
        """Map a :class:`RunResult` onto a :class:`WorkResult`, without inventing."""

        state = result.state
        declared = parse_deliverables(result.summary or "")
        artifacts = self._artifacts(role, item, result, declared)
        evidence = self._evidence(result)
        blockers = list(result.blockers)
        if result.verification is not None:
            blockers.extend(
                f"{gate.name}: {gate.summary or gate.status.value}" for gate in result.verification.blocking_failures
            )
        return WorkResult(
            item_id=item.id,
            role=role.id,
            ok=bool(result.ok),
            summary=(result.summary or "").strip()[:400] or f"{role.label} finished",
            artifacts=artifacts,
            files_touched=list(result.files_changed),
            evidence=evidence,
            steps_used=int(getattr(state.budget, "steps_used", 0) or state.step or 0),
            tokens_used=int(getattr(state.budget, "tokens_used", 0) or 0),
            error="; ".join(dict.fromkeys(blockers))[:400] or None,
            data={
                "run_id": result.run_id,
                "run_dir": str(result.run_dir),
                "status": state.status.value if hasattr(state.status, "value") else str(state.status),
                "verification_passed": bool(result.verification.passed) if result.verification else None,
                "diff_chars": len(result.diff or ""),
                "declared_outputs": sorted(declared),
            },
            follow_ups=[gate.fix_hint for gate in (result.verification.failures if result.verification else []) if gate.fix_hint],
        )

    def _artifacts(
        self, role: Role, item: WorkItem, result: RunResult, declared: dict[str, str]
    ) -> dict[str, str]:
        """Resolve each declared output from evidence first, then the worker's word."""

        wanted = list(dict.fromkeys([*(item.outputs or []), *role.outputs]))
        artifacts: dict[str, str] = {}
        for name in wanted:
            if name in _PROSE_OUTPUTS:
                # Prose deliverables (a plan, a risk list, a repo map) exist only in
                # what the worker wrote, so its declaration is the primary source and
                # evidence is a bonus.
                value = declared.get(name) or self._from_evidence(name, result)
            else:
                # Everything else is *evidence-derived*: a diff, a gate report, a
                # screenshot.  A claim is not evidence — "patch: implemented the
                # cart" over an empty diff is exactly the fabricated success this
                # layer exists to refuse, so the output stays missing and the
                # required-output check fails the worker.
                value = self._from_evidence(name, result)
            if value:
                artifacts[name] = value[:4000]
        return artifacts

    @staticmethod
    def _from_evidence(name: str, result: RunResult) -> str:
        """Objective sources: the diff, the files touched, the gate reports."""

        verification = result.verification
        diff = (result.diff or "").strip()
        files = list(result.files_changed)

        def gate(*names: str) -> str:
            if verification is None:
                return ""
            wanted = set(names)
            lines = [
                f"{entry.name}: {entry.status.value}"
                + (f" — {entry.summary}" if entry.summary else "")
                for entry in verification.results
                if not wanted or entry.name in wanted
            ]
            return "\n".join(lines)

        if name in {"patch", "diff", "changelog", "version", "artefact", "tokens", "states"}:
            if diff:
                head = diff[:3000]
                return head if not files else f"{head}\n\nfiles: {', '.join(files[:12])}"
            return ""
        if name in {"readme", "runbook", "decision_records"}:
            return f"changed: {', '.join(files[:12])}" if files else ""
        if name in {"tests", "behaviour_proof"}:
            return gate("tests")
        if name in {"baseline", "baseline_gates", "gate_report", "evidence"}:
            return gate()
        if name in {"screenshots", "console_report", "network_report", "journey_result", "a11y_notes"}:
            if verification is None:
                return ""
            browser = [entry for entry in verification.results if entry.name == "browser"]
            if not browser:
                return ""
            report = browser[0]
            detail = gate("browser")
            if report.artifacts:
                detail += "\nartifacts: " + ", ".join(str(entry) for entry in report.artifacts[:8])
            return detail if report.status is not GateStatus.SKIPPED else ""
        return ""

    def _evidence(self, result: RunResult) -> list[str]:
        """Proof lines a reviewer can check: gate outcomes and changed files."""

        evidence: list[str] = []
        if result.verification is not None:
            evidence.extend(
                f"gate {entry.name}: {entry.status.value}"
                + (f" ({entry.summary[:80]})" if entry.summary else "")
                for entry in result.verification.results
            )
        evidence.extend(f"changed {path}" for path in result.files_changed[:12])
        if result.diff:
            evidence.append(f"diff {len(result.diff)} chars")
        return evidence[:24]

    @property
    def runs(self) -> list[dict[str, Any]]:
        """Run ids produced so far — how a worker run is traced back to a run dir."""

        return list(self._runs)


#: Structural alias so type checkers see this as a :class:`WorkExecutor`.
_assert_protocol: WorkExecutor = AgentExecutor(loaded=None)  # noqa: F841 - import-time check
