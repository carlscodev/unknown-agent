"""The conductor: routing, scheduling, merging, escalation and budgets.

This is the part that makes multi-agent work *structured* rather than a crowd.
It answers four questions and refuses to guess at any of them:

**Who?**  :class:`Router` reads the task and the blueprint and picks a shape —
``solo`` for a one-file change, ``pipeline`` for ordinary feature work,
``graph`` for a greenfield build with parallel surfaces, ``escalate`` for a bug
that needs diagnosis before a fix.  Every choice is recorded with its reason.

**When?**  Work items declare ``depends_on``; the scheduler runs an item only when
its dependencies reached a terminal state, and never runs two items concurrently
whose ``owned_paths`` overlap.  Ownership that cannot be proven disjoint is
serialised — deliberately: a silent write race between two agents is the worst
failure mode this layer could have.

**What next on failure?**  Retry with the failure appended to the item's context,
then escalate to the planner for a revision, then block.  Budgets (steps, tokens,
wall clock, worker count) are checked before *every* dispatch, and when one is
exceeded the remaining work is cancelled rather than half-done.

**Did it actually work?**  Each role declares ``verify_gates``; the conductor runs
them through an injected verifier and routes a red gate back to the worker that
produced it, exactly like a human reviewer would.

Nothing here calls a model.  Execution is injected (:class:`WorkExecutor`), which
is why the whole orchestration layer is testable offline and why a solo run and a
ten-worker graph share one code path.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from unknown_agent.observability.logging import get_logger
from unknown_agent.orchestration.ledger import Ledger, ledger_path_for
from unknown_agent.orchestration.roles import ROLES, get_role, has_role
from unknown_agent.orchestration.worker import (
    OfflineExecutor,
    WorkExecutor,
    WorkItem,
    WorkResult,
    Worker,
    WorkerContext,
    WorkState,
)
from unknown_agent.planning.axes import AXES
from unknown_agent.planning.clarify import extract_signals, underspecification_score
from unknown_agent.planning.models import Blueprint

log = get_logger("orchestration.conductor")

__all__ = [
    "TaskAnalysis",
    "WorkGraph",
    "MergeOutcome",
    "OrchestrationOutcome",
    "Router",
    "Conductor",
    "Budget",
    "orchestrate",
    "DEFAULT_MILESTONES",
    "analyze_task",
    "merge_results",
    "conflicting_pairs",
]


# --------------------------------------------------------------------------- #
# task analysis and routing
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TaskAnalysis:
    """What the conductor knows about a task before it decides anything."""

    task: str
    complexity: int = 1
    surfaces: list[str] = field(default_factory=list)
    has_ui: bool = False
    has_api: bool = False
    has_data: bool = False
    has_docs: bool = False
    has_release: bool = False
    has_security: bool = False
    is_bug: bool = False
    is_greenfield: bool = False
    is_refactor: bool = False
    is_question: bool = False
    has_action: bool = True
    underspecification: float = 0.0
    preset: str | None = None
    milestones: int = 0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task[:200],
            "complexity": self.complexity,
            "surfaces": self.surfaces,
            "has_ui": self.has_ui,
            "has_api": self.has_api,
            "has_data": self.has_data,
            "has_docs": self.has_docs,
            "has_release": self.has_release,
            "has_security": self.has_security,
            "is_bug": self.is_bug,
            "is_greenfield": self.is_greenfield,
            "is_refactor": self.is_refactor,
            "is_question": self.is_question,
            "has_action": self.has_action,
            "underspecification": self.underspecification,
            "preset": self.preset,
            "milestones": self.milestones,
            "reasons": self.reasons,
        }


_SURFACE_WORDS: dict[str, tuple[str, ...]] = {
    "ui": ("ui", "page", "screen", "component", "layout", "css", "style", "design", "dashboard", "landing", "form", "button"),
    "api": ("api", "endpoint", "route", "rest", "graphql", "webhook", "service", "backend"),
    "data": ("database", "schema", "migration", "model", "table", "postgres", "sql", "orm", "query"),
    "auth": ("auth", "login", "signin", "sign-in", "session", "permission", "role", "sso", "oauth", "password"),
    "docs": ("docs", "readme", "document", "runbook", "contributing", "handover"),
    "release": ("release", "deploy", "ship", "publish", "version", "tag", "production"),
    "tests": ("test", "coverage", "spec", "pytest", "vitest"),
    "security": ("security", "audit", "vulnerability", "secret", "injection", "harden"),
    "infra": ("docker", "kubernetes", "ci", "pipeline", "terraform", "infra"),
}

_SURFACE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    name: tuple(re.compile(rf"\b{re.escape(word)}\b") for word in words) for name, words in _SURFACE_WORDS.items()
}

#: Without an action verb there is nothing to orchestrate: it is a question.
_ACTION_PATTERN = re.compile(
    r"\b(build|create|make|add|implement|write|fix|refactor|change|update|remove|delete|deploy|release|ship|"
    r"publish|improve|optimi[sz]e|optimize|migrate|scaffold|generate|design|test|debug|patch|extend|wire|"
    r"integrate|configure|set up|setup|install|document|rename|move|split|merge|upgrade|hardening|harden)\b",
    re.I,
)

_BUG_WORDS = ("fix", "bug", "broken", "failing", "crash", "error", "regression", "flaky", "500", "wrong", "not working", "traceback")
_GREENFIELD_WORDS = ("build", "create", "scaffold", "new app", "new project", "from scratch", "start a", "make me a", "i want to build")
_REFACTOR_WORDS = ("refactor", "restructure", "clean up", "simplify", "decouple", "extract", "modularize", "reorganize")


def analyze_task(task: str, *, blueprint: Blueprint | None = None, repo_known: bool = False) -> TaskAnalysis:
    """Score a task's shape.  Deterministic, offline, and explainable."""

    lowered = (task or "").lower()
    analysis = TaskAnalysis(task=task)

    # Word-boundary matching: substring matching made "api" fire inside
    # "function" and "test" inside "latest", which inflated complexity and sent
    # read-only questions into a four-worker pipeline.
    surfaces = [name for name, patterns in _SURFACE_PATTERNS.items() if any(pattern.search(lowered) for pattern in patterns)]
    analysis.surfaces = surfaces
    analysis.has_action = bool(_ACTION_PATTERN.search(lowered))
    analysis.has_ui = "ui" in surfaces
    analysis.has_api = "api" in surfaces
    analysis.has_data = "data" in surfaces or "auth" in surfaces
    analysis.has_docs = "docs" in surfaces
    analysis.has_release = "release" in surfaces
    analysis.has_security = "security" in surfaces

    analysis.is_bug = any(word in lowered for word in _BUG_WORDS)
    analysis.is_refactor = any(word in lowered for word in _REFACTOR_WORDS)
    analysis.is_greenfield = any(word in lowered for word in _GREENFIELD_WORDS) and not repo_known
    # "what does this do?" / "where is X handled?" are questions, not work items:
    # they get one read-only worker, never a pipeline.
    analysis.is_question = not analysis.has_action or bool(
        re.search(r"\b(what|where|why|how|which|who|when|is|are|does|do|can|should)\b[^.?!]*\?\s*$", lowered.strip())
    ) and not analysis.has_action

    spec = underspecification_score(task)
    analysis.underspecification = spec["score"]
    analysis.preset = spec.get("preset")

    complexity = 1
    reasons: list[str] = []
    if analysis.is_question:
        analysis.complexity = 1
        analysis.reasons = ["this is a question, not a change request — one read-only worker"]
        return analysis
    complexity += min(3, len(surfaces))
    if len(surfaces) > 1:
        reasons.append(f"touches {len(surfaces)} surfaces ({', '.join(surfaces[:4])})")
    words = len(lowered.split())
    if words > 25:
        complexity += 1
        reasons.append("the request carries a lot of detail")
    if analysis.is_greenfield:
        complexity += 3
        reasons.append("greenfield: nothing exists yet")
    if analysis.is_bug and not analysis.is_refactor:
        complexity += 1
        reasons.append("a defect needs diagnosis before a fix")
    if analysis.is_refactor:
        complexity += 1
        reasons.append("refactoring needs a baseline and a behaviour proof")
    if analysis.has_security:
        complexity += 1
        reasons.append("security work needs evidence, not review")
    if analysis.underspecification >= 0.6:
        complexity += 2
        reasons.append("the request is underspecified — planning must happen before execution")

    if blueprint is not None:
        depth = blueprint.value("feature_depth")
        scope = blueprint.value("scope_level")
        analysis.milestones = len(blueprint.answered_axes) // 4
        if depth in {"full", "platform"}:
            complexity += 2
            reasons.append(f"blueprint asks for '{depth}' depth")
        elif depth == "core":
            complexity += 1
            reasons.append("blueprint asks for a polished core, not a prototype")
        if scope in {"months-1-3", "open-ended"}:
            complexity += 2
            reasons.append(f"blueprint assumes a '{scope}' timeline")
        if blueprint.value("monetization") not in {"", "none", "free", "internal"}:
            complexity += 1
            reasons.append("billing is in scope")
        if blueprint.value("auth_strategy") not in {"", "none"}:
            complexity += 1
            reasons.append("authentication is in scope")
        if blueprint.value("integrations") not in {"", "none"}:
            complexity += 1
            reasons.append("external integrations are in scope")

    analysis.complexity = max(1, min(10, complexity))
    analysis.reasons = reasons or ["single-surface change"]
    return analysis


@dataclass(slots=True)
class WorkGraph:
    """The delegated work: items, shape, and why that shape."""

    items: list[WorkItem] = field(default_factory=list)
    pattern: str = "solo"
    rationale: str = ""
    analysis: TaskAnalysis | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def by_id(self, item_id: str) -> WorkItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def find(self, *, role: str | None = None, state: WorkState | None = None) -> list[WorkItem]:
        return [
            item
            for item in self.items
            if (role is None or item.role == role) and (state is None or item.state is state)
        ]

    def ready(self) -> list[WorkItem]:
        """Items whose dependencies are all satisfied."""

        done = {item.id for item in self.items if item.state in {WorkState.SUCCEEDED, WorkState.SKIPPED}}
        dead = {item.id for item in self.items if item.state in {WorkState.BLOCKED, WorkState.CANCELLED, WorkState.FAILED}}
        out: list[WorkItem] = []
        for item in self.items:
            if item.state is not WorkState.PENDING and item.state is not WorkState.QUEUED:
                continue
            if any(dependency in dead for dependency in item.depends_on):
                continue
            if all(dependency in done for dependency in item.depends_on):
                out.append(item)
        out.sort(key=lambda item: (-item.priority, item.role, item.id))
        return out

    def blocked_by_failure(self) -> list[WorkItem]:
        dead = {item.id for item in self.items if item.state in {WorkState.BLOCKED, WorkState.CANCELLED, WorkState.FAILED}}
        out: list[WorkItem] = []
        for item in self.items:
            if item.state in {WorkState.PENDING, WorkState.QUEUED} and any(dep in dead for dep in item.depends_on):
                out.append(item)
        return out

    def validate(self) -> list[str]:
        problems: list[str] = []
        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            problems.append("duplicate work ids")
        for item in self.items:
            if not has_role(item.role):
                problems.append(f"{item.id}: unknown role {item.role!r}")
            for dependency in item.depends_on:
                if dependency not in ids:
                    problems.append(f"{item.id}: depends on unknown item {dependency!r}")
                elif dependency == item.id:
                    problems.append(f"{item.id}: depends on itself")
        # cycle check
        graph = {item.id: list(item.depends_on) for item in self.items}
        colour: dict[str, int] = {}

        def visit(node: str, stack: tuple[str, ...]) -> bool:
            if colour.get(node) == 2:
                return False
            if colour.get(node) == 1 or node in stack:
                return True
            colour[node] = 1
            for parent in graph.get(node, ()):
                if visit(parent, stack + (node,)):
                    return True
            colour[node] = 2
            return False

        for item_id in graph:
            if visit(item_id, ()):
                problems.append(f"dependency cycle involving {item_id}")
                break
        return problems

    def summary(self) -> dict[str, Any]:
        per_role: dict[str, int] = {}
        for item in self.items:
            per_role[item.role] = per_role.get(item.role, 0) + 1
        return {
            "pattern": self.pattern,
            "items": len(self.items),
            "roles": per_role,
            "rationale": self.rationale,
            "warnings": self.warnings,
            "states": {item.id: item.state.value for item in self.items},
            "dependencies": {item.id: item.depends_on for item in self.items},
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.summary()
        payload["work"] = [item.to_dict() for item in self.items]
        if self.analysis is not None:
            payload["analysis"] = self.analysis.to_dict()
        return payload


#: The deliverable groups a greenfield build gets when the caller has no plan yet.
#: Two, not one: "it runs" and "the journey works" are different definitions of done,
#: and splitting them is what lets the second worker be verified against the first.
DEFAULT_MILESTONES: tuple[dict[str, Any], ...] = (
    {
        "id": "m0",
        "title": "Foundation",
        "outcome": "A runnable skeleton",
        "tasks": ["scaffold", "configure"],
        "acceptance": ["it builds and runs"],
    },
    {
        "id": "m1",
        "title": "Core behaviour",
        "outcome": "The primary journey works",
        "tasks": ["implement core"],
        "acceptance": ["the journey completes"],
    },
)


def conflicting_pairs(
    items: Iterable[WorkItem], *, respect_dependencies: bool = True
) -> list[tuple[str, str, str]]:
    """Pairs of items that both mutate and could touch the same path.

    Two globs conflict when either matches the other's pattern or they share a
    literal prefix.  Conservative by design: a false positive serialises two
    workers (slow, safe); a false negative lets them race (fast, corrupting).

    The invariant is about *concurrency*, so by default a pair that is already
    ordered by a dependency chain is not a conflict — it cannot run at the same
    time.  Pass ``respect_dependencies=False`` to see raw ownership overlaps,
    which is what the router uses before it adds those edges.
    """

    materialised = list(items)
    ordered: set[tuple[str, str]] = set()
    if respect_dependencies:
        by_id = {item.id: item for item in materialised}

        def upstream(item_id: str, seen: set[str]) -> set[str]:
            item = by_id.get(item_id)
            if item is None:
                return set()
            found: set[str] = set()
            for parent in item.depends_on:
                if parent in seen:
                    continue
                seen.add(parent)
                found.add(parent)
                found |= upstream(parent, seen)
            return found

        for item in materialised:
            for ancestor in upstream(item.id, set()):
                ordered.add((ancestor, item.id))

    mutating = [item for item in materialised if get_role(item.role).mutates]
    out: list[tuple[str, str, str]] = []
    for index, first in enumerate(mutating):
        for second in mutating[index + 1 :]:
            if (first.id, second.id) in ordered or (second.id, first.id) in ordered:
                continue
            for left in first.owned_paths or ["**"]:
                for right in second.owned_paths or ["**"]:
                    if _globs_overlap(left, right):
                        out.append((first.id, second.id, f"{left} ∩ {right}"))
                        break
                else:
                    continue
                break
    return out


def _globs_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    if left == "**" or right == "**":
        return True
    if fnmatch.fnmatch(right, left) or fnmatch.fnmatch(left, right):
        return True
    left_prefix = left.split("**")[0].rstrip("/")
    right_prefix = right.split("**")[0].rstrip("/")
    if left_prefix and right_prefix and (left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)):
        return True
    left_ext = left.rsplit(".", 1)[-1] if "." in left.rsplit("/", 1)[-1] else ""
    right_ext = right.rsplit(".", 1)[-1] if "." in right.rsplit("/", 1)[-1] else ""
    return bool(left_ext) and left_ext == right_ext


#: Which roles survive a budget that is too small for the plan.
#:
#: Doing and proving beat planning and describing: a single coder produces
#: something, a single planner produces a document about something.  Sorting by
#: ``priority`` alone got this wrong — the planner (priority 10) and the inspector
#: (9) out-ranked the coder (7), so a tight budget kept the two workers that cannot
#: change a file and dropped the one that can.
_KEEP_RANK: dict[str, int] = {
    "coder": 0,
    "refactorer": 1,
    "ux-finisher": 2,
    "verifier": 3,
    "documenter": 4,
    "browser-validator": 5,
    "releaser": 6,
    "tooling": 7,
    "planner": 8,
    "inspector": 9,
}


def _keep_rank(item: WorkItem) -> tuple[int, int, int, float]:
    """Sort key for "who stays when the budget is too small" (lower is kept)."""

    return (
        0 if item.required else 1,
        _KEEP_RANK.get(item.role, 4),
        -item.priority,
        item.created_at,
    )


class Router:
    """Chooses the orchestration shape and builds the work graph."""

    def __init__(self, *, config: Any = None) -> None:
        self.config = config

    # -- config accessors (duck-typed so this works with or without settings) --
    def _cfg(self, name: str, default: Any) -> Any:
        return getattr(self.config, name, default) if self.config is not None else default

    def pattern_for(self, analysis: TaskAnalysis, *, blueprint: Blueprint | None = None) -> tuple[str, str]:
        """Return ``(pattern, rationale)``."""

        forced = self._cfg("pattern", None)
        if forced:
            return str(forced), f"pattern forced by [orchestration].pattern = {forced!r}"
        mode = str(self._cfg("mode", "auto"))
        if mode == "solo":
            return "solo", "orchestration mode is 'solo': one worker, no delegation"
        enabled = bool(self._cfg("enabled", True))
        if not enabled:
            return "solo", "orchestration is disabled in config"
        minimum = int(self._cfg("min_complexity", 3))
        if analysis.is_question:
            return "solo", "this is a question: one read-only worker answers it"
        if mode == "auto" and analysis.complexity < minimum:
            return "solo", f"complexity {analysis.complexity}/10 is below the orchestration threshold ({minimum})"

        if analysis.is_greenfield or (blueprint is not None and analysis.milestones >= 2):
            groups = analysis.milestones or len(DEFAULT_MILESTONES)
            return "graph", f"greenfield work with {groups} deliverable groups needs parallel, dependency-ordered workers"
        if analysis.is_bug and not analysis.is_refactor:
            return "escalate", "a defect needs diagnosis → fix → proof, with replanning when the first hypothesis is wrong"
        if analysis.is_refactor:
            return "pipeline", "refactoring is strictly sequential: baseline → change → prove behaviour unchanged"
        parallel_surfaces = sum(1 for flag in (analysis.has_ui, analysis.has_api, analysis.has_data) if flag)
        if parallel_surfaces >= 2 or analysis.has_release or analysis.has_docs:
            return "fanout", f"{parallel_surfaces} independent surfaces can be worked in parallel and merged"
        return "pipeline", "ordinary feature work: inspect → plan → implement → verify"

    def build(
        self,
        task: str,
        *,
        analysis: TaskAnalysis | None = None,
        blueprint: Blueprint | None = None,
        milestones: Sequence[Mapping[str, Any]] = (),
        available_roles: Sequence[str] = (),
        repo_known: bool = False,
    ) -> WorkGraph:
        """Build the work graph for a task."""

        resolved = analysis or analyze_task(task, blueprint=blueprint, repo_known=repo_known)
        pattern, rationale = self.pattern_for(resolved, blueprint=blueprint)
        allowed = self._allowed_roles(available_roles)
        graph = WorkGraph(pattern=pattern, rationale=rationale, analysis=resolved)

        builder = {
            "solo": self._solo,
            "pipeline": self._pipeline,
            "fanout": self._fanout,
            "graph": self._graph,
            "escalate": self._escalate,
        }.get(pattern, self._pipeline)
        builder(graph, task, resolved, blueprint, milestones, allowed)

        self._serialise_conflicts(graph)
        self._fit_budget(graph)
        problems = graph.validate()
        graph.warnings.extend(problems)
        if problems:
            log.warning("work graph has structural problems: %s", "; ".join(problems))
        return graph

    # -- role availability -------------------------------------------------- #
    def _allowed_roles(self, available: Sequence[str]) -> set[str]:
        allow = {str(item) for item in (self._cfg("allow_roles", []) or [])}
        deny = {str(item) for item in (self._cfg("deny_roles", []) or [])}
        roles = set(ROLES)
        if allow:
            roles &= allow
        roles -= deny
        if available:
            roles &= set(available) | {"planner", "verifier"}
        for key, value in (self._cfg("roles", {}) or {}).items():
            settings = value
            if getattr(settings, "enabled", True) is False:
                roles.discard(key)
        return roles or {"coder"}

    def _role_settings(self, role_id: str) -> Any:
        settings = (self._cfg("roles", {}) or {}).get(role_id)
        return settings

    # -- graph builders ----------------------------------------------------- #
    def _item(
        self,
        graph: WorkGraph,
        role: str,
        task: str,
        *,
        context: str = "",
        depends_on: Sequence[str] = (),
        owned_paths: Sequence[str] = (),
        outputs: Sequence[str] = (),
        acceptance: Sequence[str] = (),
        priority: int = 5,
        group: str = "",
        required: bool | None = None,
        max_steps: int | None = None,
    ) -> WorkItem:
        card = get_role(role)
        overrides = self._role_settings(role)
        item = WorkItem(
            role=card.id,
            task=task,
            context=context,
            depends_on=list(depends_on),
            owned_paths=list(owned_paths or card.owned_paths),
            outputs=list(outputs or card.outputs),
            acceptance=list(acceptance),
            priority=priority,
            parallel_group=group,
            required=card.required if required is None else required,
            max_steps=max_steps or int(getattr(overrides, "max_steps", 0) or card.max_steps or self._cfg("worker_max_steps", 24)),
            reasoning_mode=getattr(overrides, "reasoning_mode", None) or None,
        )
        graph.items.append(item)
        return item

    def _solo(
        self,
        graph: WorkGraph,
        task: str,
        analysis: TaskAnalysis,
        blueprint: Blueprint | None,
        milestones: Sequence[Mapping[str, Any]],
        allowed: set[str],
    ) -> None:
        if analysis.is_question and "inspector" in allowed:
            self._item(
                graph,
                "inspector",
                task,
                priority=9,
                acceptance=["the answer cites the files and lines it came from"],
            )
            return
        role = "coder" if "coder" in allowed else sorted(allowed)[0]
        self._item(graph, role, task, priority=9, acceptance=["the change is complete and the relevant gates pass"])

    def _pipeline(
        self,
        graph: WorkGraph,
        task: str,
        analysis: TaskAnalysis,
        blueprint: Blueprint | None,
        milestones: Sequence[Mapping[str, Any]],
        allowed: set[str],
    ) -> None:
        previous: list[str] = []
        if "inspector" in allowed:
            inspect_item = self._item(
                graph,
                "inspector",
                f"Map the repository and record the conventions this work must follow: {task[:120]}",
                priority=8,
            )
            previous = [inspect_item.id]
        if "planner" in allowed and (analysis.complexity >= 4 or analysis.underspecification >= 0.5):
            plan_item = self._item(
                graph,
                "planner",
                f"Produce an execution plan with acceptance criteria for: {task[:160]}",
                depends_on=previous,
                priority=8,
            )
            previous = previous + [plan_item.id]
        if "coder" in allowed:
            code_item = self._item(
                graph,
                "coder",
                task,
                context="follow the inspector's conventions and the planner's acceptance criteria",
                depends_on=previous,
                priority=7,
            )
            previous = previous + [code_item.id]
        self._tail(graph, task, analysis, previous, allowed)

    def _fanout(
        self,
        graph: WorkGraph,
        task: str,
        analysis: TaskAnalysis,
        blueprint: Blueprint | None,
        milestones: Sequence[Mapping[str, Any]],
        allowed: set[str],
    ) -> None:
        trunk: list[str] = []
        if "inspector" in allowed:
            trunk.append(self._item(graph, "inspector", f"Map the repository before parallel work begins: {task[:120]}", priority=9).id)
        if "planner" in allowed:
            trunk.append(
                self._item(
                    graph,
                    "planner",
                    f"Split this work into independently mergeable pieces: {task[:160]}",
                    depends_on=trunk,
                    priority=9,
                ).id
            )
        branches: list[str] = []
        if analysis.has_data and "coder" in allowed:
            branches.append(
                self._item(
                    graph,
                    "coder",
                    f"Data layer for: {task[:140]}",
                    depends_on=trunk,
                    group="data",
                    owned_paths=("**/models/**", "**/migrations/**", "**/db/**", "**/schema*"),
                    acceptance=["migrations apply forward and backward", "round-trip tests pass"],
                    priority=7,
                ).id
            )
        if analysis.has_api and "coder" in allowed:
            branches.append(
                self._item(
                    graph,
                    "coder",
                    f"API surface for: {task[:140]}",
                    depends_on=trunk,
                    group="api",
                    owned_paths=("**/api/**", "**/routes/**", "**/endpoints/**", "**/handlers/**"),
                    acceptance=["every endpoint has an authorisation test", "errors use the documented envelope"],
                    priority=7,
                ).id
            )
        if analysis.has_ui and "ux-finisher" in allowed:
            branches.append(
                self._item(
                    graph,
                    "ux-finisher",
                    f"UI for: {task[:140]}",
                    depends_on=trunk,
                    group="ui",
                    acceptance=["loading, empty, error and disabled states exist", "layout holds at 360px and 1440px"],
                    priority=6,
                ).id
            )
        elif "coder" in allowed:
            branches.append(self._item(graph, "coder", task, depends_on=trunk, group="main", priority=7).id)
        if not branches and "coder" in allowed:
            branches.append(self._item(graph, "coder", task, depends_on=trunk, priority=7).id)
        self._tail(graph, task, analysis, trunk + branches, allowed)

    def _graph(
        self,
        graph: WorkGraph,
        task: str,
        analysis: TaskAnalysis,
        blueprint: Blueprint | None,
        milestones: Sequence[Mapping[str, Any]],
        allowed: set[str],
    ) -> None:
        """Greenfield: plan, then one worker per milestone, ordered by dependency."""

        plan_id = ""
        if "planner" in allowed:
            plan_id = self._item(
                graph,
                "planner",
                f"Produce the blueprint-driven build plan for: {task[:180]}",
                context=(blueprint.spec_text(max_chars=1400) if blueprint else ""),
                priority=10,
                acceptance=["every milestone has executable acceptance criteria", "risks are named with mitigations"],
            ).id
        inspect_id = ""
        if "inspector" in allowed:
            inspect_id = self._item(
                graph,
                "inspector",
                "Record the current tree state, toolchain and commands so the build starts from facts",
                depends_on=[plan_id] if plan_id else [],
                priority=9,
            ).id

        milestone_items: list[str] = []
        entries = list(milestones) or list(DEFAULT_MILESTONES)
        previous_milestone = inspect_id or plan_id
        for index, milestone in enumerate(entries):
            if "coder" not in allowed:
                break
            mid = str(milestone.get("id") or f"m{index}")
            title = str(milestone.get("title") or mid)
            outcome = str(milestone.get("outcome") or title)
            tasks = [str(item) for item in (milestone.get("tasks") or [])]
            acceptance = [str(item) for item in (milestone.get("acceptance") or [])] or [outcome]
            deps = [previous_milestone] if previous_milestone else []
            item = self._item(
                graph,
                "coder",
                f"{title}: {outcome}",
                context="\n".join(f"- {entry}" for entry in tasks[:8]),
                depends_on=deps,
                group=f"milestone:{mid}",
                acceptance=acceptance[:6],
                priority=max(3, 8 - index),
            )
            milestone_items.append(item.id)
            previous_milestone = item.id

        if analysis.has_ui and "ux-finisher" in allowed and milestone_items:
            milestone_items.append(
                self._item(
                    graph,
                    "ux-finisher",
                    "Design pass: tokens, states, hierarchy, contrast, responsive behaviour",
                    depends_on=milestone_items[-1:],
                    group="polish",
                    priority=5,
                ).id
            )
        self._tail(graph, task, analysis, milestone_items, allowed)

    def _escalate(
        self,
        graph: WorkGraph,
        task: str,
        analysis: TaskAnalysis,
        blueprint: Blueprint | None,
        milestones: Sequence[Mapping[str, Any]],
        allowed: set[str],
    ) -> None:
        """Diagnosis before treatment: inspect → hypothesise → fix → prove."""

        ids: list[str] = []
        if "inspector" in allowed:
            ids.append(
                self._item(
                    graph,
                    "inspector",
                    f"Reproduce and localise the defect: {task[:160]}",
                    priority=9,
                    acceptance=["a reproduction exists", "the failing component is named with evidence"],
                ).id
            )
        if "planner" in allowed:
            ids.append(
                self._item(
                    graph,
                    "planner",
                    "State the hypothesis, the falsifying evidence, and the smallest fix that follows from it",
                    depends_on=ids,
                    priority=9,
                ).id
            )
        if "coder" in allowed:
            ids.append(
                self._item(
                    graph,
                    "coder",
                    f"Apply the smallest fix and add the regression test: {task[:140]}",
                    depends_on=ids,
                    priority=8,
                    acceptance=["a test fails on the old code and passes on the new", "no unrelated change in the diff"],
                ).id
            )
        self._tail(graph, task, analysis, ids, allowed)

    def _tail(self, graph: WorkGraph, task: str, analysis: TaskAnalysis, after: Sequence[str], allowed: set[str]) -> None:
        """The verification / browser / docs / release tail every shape shares."""

        deps = list(after)
        if analysis.has_ui and "browser-validator" in allowed:
            browser = self._item(
                graph,
                "browser-validator",
                "Drive the real browser through the core journey and capture console, network and screenshot evidence",
                depends_on=deps,
                priority=6,
                required=False,
            )
            deps = deps + [browser.id]
        if "verifier" in allowed:
            verifier = self._item(
                graph,
                "verifier",
                "Run the definition of done and report which gates are green with evidence attached",
                depends_on=deps,
                priority=8,
            )
            deps = deps + [verifier.id]
        if analysis.has_docs and "documenter" in allowed:
            deps = deps + [
                self._item(
                    graph,
                    "documenter",
                    "Write or update the documentation this change requires, verifying every command by running it",
                    depends_on=deps,
                    priority=4,
                    required=False,
                ).id
            ]
        if analysis.has_release and "releaser" in allowed:
            deps = deps + [
                self._item(
                    graph,
                    "releaser",
                    "Prepare the release: version, changelog, artefact, dry-run deploy, rollback plan",
                    depends_on=deps,
                    priority=3,
                    required=False,
                ).id
            ]

    def _fit_budget(self, graph: WorkGraph) -> None:
        """Guarantee the plan fits ``[orchestration].max_workers``.

        A conductor that dispatches more workers than the budget allows has to
        cancel work mid-run, which is the worst possible outcome: the user gets a
        partial product and a confusing ledger.  Trimming here is honest and
        reviewable, and it happens in a defined order:

        1. optional roles go first (docs, release, browser validation),
        2. then duplicate roles beyond the first (a second ``coder``),
        3. if it still does not fit, the shape collapses to ``solo`` — one worker
           doing the whole task — because one honest worker beats three cancelled ones.

        Every trim is recorded in ``graph.warnings`` so ``ua orchestrate plan``
        shows what was dropped and why.
        """

        cap = int(self._cfg("max_workers", 8) or 8)
        if cap <= 0 or len(graph.items) <= cap:
            return

        def drop(item: WorkItem, reason: str) -> None:
            if item not in graph.items:
                return
            graph.items.remove(item)
            for other in graph.items:
                other.depends_on = [dep for dep in other.depends_on if dep != item.id]
            graph.warnings.append(f"dropped {item.role} '{item.id}' to fit max_workers={cap}: {reason}")

        optional_order = ("documenter", "releaser", "browser-validator", "refactorer", "tooling")
        for role_id in optional_order:
            while len(graph.items) > cap:
                candidate = next((item for item in reversed(graph.items) if item.role == role_id), None)
                if candidate is None:
                    break
                drop(candidate, "optional role")

        seen: dict[str, int] = {}
        for item in list(graph.items):
            seen[item.role] = seen.get(item.role, 0) + 1
            if len(graph.items) <= cap:
                break
            if seen[item.role] > 1 and item.role in {"coder", "inspector", "planner"}:
                drop(item, f"redundant {item.role} worker")

        if len(graph.items) > cap:
            # Trimming optional and redundant workers was not enough.  Two honest
            # outcomes, and which one applies depends on the cap:
            #
            # * cap == 1 → genuinely solo.  One worker carries the whole request and
            #   inherits the acceptance criteria of everyone it absorbed, so the
            #   things the plan cared about are still checked rather than lost.
            # * cap > 1  → keep the highest-priority workers in stage order and say
            #   the plan was trimmed.  Relabelling a four-worker graph "solo" is the
            #   worst option: the shape would disagree with the graph it describes.
            if cap <= 1:
                survivor = next((item for item in graph.items if item.role == "coder"), None) or max(
                    graph.items, key=lambda item: (item.priority, -get_role(item.role).stage)
                )
                absorbed = [item for item in graph.items if item is not survivor]
                for item in absorbed:
                    for criterion in item.acceptance:
                        if criterion not in survivor.acceptance:
                            survivor.acceptance.append(criterion)
                survivor.depends_on = []
                survivor.task = (
                    f"{survivor.task} (collapsed: also covers "
                    f"{', '.join(get_role(item.role).label for item in absorbed[:4])})"
                )[:400]
                graph.items = [survivor]
                graph.pattern = "solo"
                graph.rationale = f"collapsed to one worker to respect max_workers={cap}"
                graph.warnings.append(
                    f"plan exceeded max_workers={cap}; collapsed to one worker "
                    f"({get_role(survivor.role).label}) carrying {len(survivor.acceptance)} acceptance criteria"
                )
            else:
                graph.items.sort(key=lambda item: _keep_rank(item))
                dropped = graph.items[cap:]
                graph.items = graph.items[:cap]
                graph.items.sort(key=lambda item: (get_role(item.role).stage, item.created_at))
                for item in graph.items:
                    item.depends_on = [dep for dep in item.depends_on if any(other.id == dep for other in graph.items)]
                graph.rationale = f"{graph.rationale} (trimmed to {cap} workers to respect max_workers={cap})"
                graph.warnings.append(
                    f"plan exceeded max_workers={cap}; kept {', '.join(get_role(item.role).label for item in graph.items)} "
                    f"and dropped {', '.join(get_role(item.role).label for item in dropped)}"
                )

    def _serialise_conflicts(self, graph: WorkGraph) -> None:
        """Owns-path overlap between two mutating workers becomes a dependency.

        This is the safety property that makes concurrency acceptable: if we cannot
        prove two workers touch disjoint files, they do not run at the same time.
        """

        for first_id, second_id, overlap in conflicting_pairs(graph.items, respect_dependencies=False):
            first = graph.by_id(first_id)
            second = graph.by_id(second_id)
            if first is None or second is None:
                continue
            if first.id in second.depends_on or second.id in first.depends_on:
                continue
            # keep stage order: the earlier stage leads
            lead, follow = (first, second) if get_role(first.role).stage <= get_role(second.role).stage else (second, first)
            if lead.id in follow.depends_on:
                continue
            follow.depends_on.append(lead.id)
            graph.warnings.append(
                f"{follow.role} '{follow.id}' serialised after {lead.role} '{lead.id}': overlapping ownership ({overlap})"
            )


# --------------------------------------------------------------------------- #
# budgets
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Budget:
    """Global orchestration budget, checked before every dispatch.

    Two different kinds of limit live here, and confusing them cancels good runs:

    * ``max_workers`` is a **plan-size** limit.  The router trims the graph to it
      before dispatch, so reaching it exactly is the normal case, and a *retry* of
      a worker already counted is not a new worker.  Counting attempts instead of
      distinct workers charged retries twice and cancelled runs that had work left
      to do; comparing with ``>=`` cancelled runs that had simply finished
      dispatching.  Both are fixed: distinct work ids, and exhaustion only when the
      count *exceeds* the cap.
    * ``max_steps`` / ``max_tokens`` / ``max_wall_clock_s`` are **consumables**.
      They accumulate per attempt, which is correct: a retry really does spend more
      steps and more tokens.
    """

    max_workers: int = 8
    max_steps: int = 400
    max_tokens: int = 900_000
    max_wall_clock_s: float = 2700.0
    workers_used: int = 0
    steps_used: int = 0
    tokens_used: int = 0
    started_at: float = field(default_factory=time.time)
    warnings: list[str] = field(default_factory=list)
    #: Distinct work ids charged against ``max_workers`` (a retry is not a new one).
    work_ids: set[str] = field(default_factory=set)

    @property
    def wall_clock_s(self) -> float:
        return round(time.time() - self.started_at, 2)

    def exhausted(self) -> str | None:
        if self.workers_used > self.max_workers:
            return f"worker budget exhausted ({self.workers_used}/{self.max_workers})"
        if self.steps_used >= self.max_steps:
            return f"step budget exhausted ({self.steps_used}/{self.max_steps})"
        if self.tokens_used >= self.max_tokens:
            return f"token budget exhausted ({self.tokens_used:,}/{self.max_tokens:,})"
        if self.wall_clock_s >= self.max_wall_clock_s:
            return f"wall-clock budget exhausted ({self.wall_clock_s:.0f}s/{self.max_wall_clock_s:.0f}s)"
        return None

    def pressure(self) -> float:
        """0-1: how close the tightest *consumable* budget is to exhaustion.

        The worker count is deliberately excluded: the router trims the plan to
        ``max_workers``, so a fully dispatched plan sits at 100% by construction and
        would warn on every single run.  Pressure should mean "we are about to run
        out of room to work", not "we used the plan we were given".
        """

        ratios = [
            self.steps_used / max(1, self.max_steps),
            self.tokens_used / max(1, self.max_tokens),
            self.wall_clock_s / max(1.0, self.max_wall_clock_s),
        ]
        return round(max(ratios), 3)

    def consume(self, result: WorkResult) -> None:
        """Charge one attempt: steps and tokens always, the worker slot once."""

        if result.item_id:
            self.work_ids.add(result.item_id)
            self.workers_used = len(self.work_ids)
        self.steps_used += int(result.steps_used or 0)
        self.tokens_used += int(result.tokens_used or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers_used": self.workers_used,
            "max_workers": self.max_workers,
            "steps_used": self.steps_used,
            "max_steps": self.max_steps,
            "tokens_used": self.tokens_used,
            "max_tokens": self.max_tokens,
            "wall_clock_s": self.wall_clock_s,
            "max_wall_clock_s": self.max_wall_clock_s,
            "pressure": self.pressure(),
            "exhausted": self.exhausted(),
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# merging
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class MergeOutcome:
    """The result of combining worker artefacts."""

    strategy: str
    artifacts: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "artifacts": list(self.artifacts),
            "files": self.files,
            "conflicts": self.conflicts,
            "notes": self.notes,
            "ok": self.ok,
        }


def merge_results(results: Sequence[WorkResult], *, strategy: str = "owner_partition") -> MergeOutcome:
    """Combine worker outputs into one coherent result.

    ``owner_partition`` is the default because it is the only strategy that can
    *detect* a problem: two workers claiming the same file is a conflict, not a
    merge.  ``sequential_apply`` trusts the conductor's ordering, ``union`` keeps
    everything and reports duplicates, ``best_of`` picks by evidence count.
    """

    outcome = MergeOutcome(strategy=strategy)
    claims: dict[str, list[str]] = {}
    for result in results:
        if not result.ok:
            outcome.notes.append(f"[{result.role}] failed: {result.error or 'unknown'} — excluded from the merge")
            continue
        for key, value in result.artifacts.items():
            namespaced = f"{result.role}:{key}"
            if key in outcome.artifacts and strategy in {"owner_partition", "sequential_apply"}:
                outcome.artifacts[namespaced] = value
                outcome.notes.append(f"'{key}' produced by both an earlier worker and {result.role}; kept both under namespaced keys")
            else:
                outcome.artifacts[key] = value
        for path in result.files_touched:
            claims.setdefault(path, []).append(result.role)
            if path not in outcome.files:
                outcome.files.append(path)

    for path, roles in sorted(claims.items()):
        if len(set(roles)) > 1:
            outcome.conflicts.append({"path": path, "roles": sorted(set(roles))})

    if strategy == "best_of":
        best = max((result for result in results if result.ok), key=lambda r: len(r.evidence), default=None)
        if best is not None:
            outcome.artifacts = dict(best.artifacts)
            outcome.files = list(best.files_touched)
            outcome.notes.append(f"best_of selected {best.role} ({len(best.evidence)} evidence items)")
    if outcome.conflicts and strategy == "owner_partition":
        outcome.ok = False
        outcome.notes.append(
            "ownership conflict: " + "; ".join(f"{c['path']} claimed by {', '.join(c['roles'])}" for c in outcome.conflicts[:4])
        )
    return outcome


# --------------------------------------------------------------------------- #
# the conductor
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class OrchestrationOutcome:
    """Everything a conducted run produced."""

    run_id: str
    pattern: str
    graph: WorkGraph
    results: dict[str, WorkResult] = field(default_factory=dict)
    merge: MergeOutcome | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    #: Workers whose gates were never run because no verifier was supplied.  A run
    #: that checked nothing must not look identical to a run that passed everything.
    unverified: list[str] = field(default_factory=list)
    budget: Budget | None = None
    ok: bool = False
    status: str = "pending"  # completed | partial | blocked | failed | cancelled
    summary: str = ""
    blockers: list[str] = field(default_factory=list)
    escalations: int = 0
    retries: int = 0
    duration_s: float = 0.0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def succeeded(self) -> list[WorkItem]:
        return self.graph.find(state=WorkState.SUCCEEDED)

    def failed(self) -> list[WorkItem]:
        return [item for item in self.graph.items if item.state in {WorkState.FAILED, WorkState.BLOCKED}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pattern": self.pattern,
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "blockers": self.blockers,
            "escalations": self.escalations,
            "retries": self.retries,
            "duration_s": round(self.duration_s, 2),
            "graph": self.graph.to_dict(),
            "results": {key: value.to_dict() for key, value in self.results.items()},
            "merge": self.merge.to_dict() if self.merge else None,
            "verification": self.verification,
            "unverified": self.unverified,
            "budget": self.budget.to_dict() if self.budget else None,
        }


class Conductor:
    """Executes a :class:`WorkGraph` with budgets, retries, escalation and audit."""

    def __init__(
        self,
        *,
        config: Any = None,
        executor: WorkExecutor | None = None,
        context: WorkerContext | None = None,
        ledger: Ledger | None = None,
        verifier: Callable[[Sequence[str], WorkItem], Any] | None = None,
        on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
        router: Router | None = None,
        run_id: str = "",
    ) -> None:
        self.config = config
        self.executor = executor or OfflineExecutor()
        self.context = context or WorkerContext()
        self.ledger = ledger or Ledger(enabled=False)
        self.verifier = verifier
        self.on_event = on_event
        self.router = router or Router(config=config)
        self.run_id = run_id or f"orc-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self._replans = 0

    # -- config ------------------------------------------------------------- #
    def _cfg(self, name: str, default: Any) -> Any:
        value = getattr(self.config, name, default) if self.config is not None else default
        return default if value is None else value

    def _budget(self, graph: WorkGraph) -> Budget:
        return Budget(
            max_workers=int(self._cfg("max_workers", 8)),
            max_steps=int(self._cfg("worker_max_steps", 24)) * max(1, len(graph.items)),
            max_tokens=int(self._cfg("worker_token_budget", 180_000)) * max(1, min(4, len(graph.items))),
            max_wall_clock_s=float(self._cfg("max_wall_clock_s", 2700.0) or 2700.0),
        )

    def _emit(self, kind: str, **data: Any) -> None:
        work_id = str(data.pop("work_id", "") or "")
        role = str(data.pop("role", "") or "")
        self.ledger.record(kind, work_id=work_id, role=role, **data)
        if self.on_event is not None:
            try:
                self.on_event(kind, {"work_id": work_id, "role": role, **data})
            except Exception:  # pragma: no cover - observers must not break a run
                log.debug("orchestration event sink failed for %s", kind)

    # -- execution ---------------------------------------------------------- #
    async def run(
        self,
        task: str,
        *,
        graph: WorkGraph | None = None,
        blueprint: Blueprint | None = None,
        milestones: Sequence[Mapping[str, Any]] = (),
        repo_known: bool = False,
    ) -> OrchestrationOutcome:
        started = time.time()
        analysis = analyze_task(task, blueprint=blueprint, repo_known=repo_known)
        resolved = graph or self.router.build(
            task, analysis=analysis, blueprint=blueprint, milestones=milestones, repo_known=repo_known
        )
        budget = self._budget(resolved)
        outcome = OrchestrationOutcome(run_id=self.run_id, pattern=resolved.pattern, graph=resolved, budget=budget)

        problems = resolved.validate()
        if problems:
            # Refuse before dispatching anything.  Starting a run whose graph has a
            # cycle or a dangling dependency produces a "failed" outcome with no
            # explanation, which is the worst possible answer to "what happened?".
            outcome.status = "failed"
            outcome.blockers.extend(problems)
            outcome.summary = f"{resolved.pattern} graph is not executable: {'; '.join(problems[:2])}"
            self._emit("run_started", pattern=resolved.pattern, items=len(resolved.items))
            self._emit("note", note="graph rejected before dispatch", problems=problems[:4])
            self._emit("run_finished", status=outcome.status, ok=False, reason="invalid graph")
            outcome.finished_at = time.time()
            outcome.duration_s = round(outcome.finished_at - started, 2)
            return outcome

        if blueprint is not None:
            self.context.blueprint_text = blueprint.spec_text(max_chars=1600)
        self.context.task = task

        self._emit("run_started", pattern=resolved.pattern, items=len(resolved.items), complexity=analysis.complexity)
        self._emit("plan_selected", pattern=resolved.pattern, note=resolved.rationale)
        for item in resolved.items:
            self._emit("work_created", work_id=item.id, role=item.role, task=item.task[:160], depends_on=item.depends_on)

        concurrency = max(1, int(self._cfg("concurrency", 2)))
        max_retries = int(self._cfg("max_retries_per_worker", 2))
        max_replans = int(self._cfg("max_replans", 2))

        while True:
            ready = resolved.ready()
            if not ready:
                break
            reason = budget.exhausted()
            if reason:
                self._emit("budget_exhausted", note=reason)
                for item in resolved.find(state=WorkState.PENDING) + resolved.find(state=WorkState.QUEUED):
                    item.transition(WorkState.CANCELLED, note=reason)
                    self._emit("work_cancelled", work_id=item.id, role=item.role, reason=reason)
                outcome.status = "cancelled"
                outcome.blockers.append(reason)
                break

            # Batch selection: run up to ``concurrency`` items at once, but never two
            # mutating items whose ownership globs overlap.  Proving disjointness is
            # the price of parallelism; when it cannot be proved, one waits.
            batch: list[WorkItem] = []
            claimed: list[tuple[str, list[str]]] = []
            for item in ready:
                if len(batch) >= concurrency:
                    break
                mutates = get_role(item.role).mutates
                paths = list(item.owned_paths or (["**"] if mutates else []))
                if mutates and any(
                    _globs_overlap(left, right) for left in paths for other_paths in claimed for right in other_paths[1]
                ):
                    continue
                batch.append(item)
                if paths:
                    claimed.append((item.id, paths))

            if not batch:
                # Everything ready conflicts with something already chosen: take the
                # highest-priority one alone rather than stalling the run.
                batch = ready[:1]

            for item in batch:
                item.transition(WorkState.QUEUED, note=f"dispatched (batch of {len(batch)})")
                self._emit("work_queued", work_id=item.id, role=item.role)

            results = await asyncio.gather(*(self._execute(item, outcome) for item in batch))

            for item, result in zip(batch, results):
                outcome.results[item.id] = result
                budget.consume(result)
                if budget.pressure() >= 0.8 and not budget.warnings:
                    budget.warnings.append(f"budget pressure at {int(budget.pressure() * 100)}%")
                    self._emit("budget_warning", pressure=budget.pressure())

                if result.ok:
                    gates = list(get_role(item.role).verify_gates)
                    if self._cfg("verify_each_worker", True) and gates:
                        if self.verifier is None:
                            # No verifier means nobody ran the gates.  Recording that
                            # explicitly is what keeps "we did not check" from reading
                            # as "it passed".
                            outcome.verification[item.id] = {
                                "ok": None,
                                "checked": False,
                                "gates": gates,
                                "note": "no verifier supplied; gates were not run",
                            }
                            if item.id not in outcome.unverified:
                                outcome.unverified.append(item.id)
                            self._emit(
                                "verify_finished",
                                work_id=item.id,
                                role=item.role,
                                gates=gates,
                                checked=False,
                                note="no verifier supplied",
                            )
                        else:
                            passed = await self._verify(item, result, outcome)
                            if not passed:
                                continue
                elif item.can_retry and item.attempts <= max_retries:
                    item.context = "\n".join(
                        part for part in (item.context, f"PREVIOUS ATTEMPT FAILED: {result.error or result.summary}") if part
                    )
                    item.transition(WorkState.QUEUED, note="retry after failure")
                    outcome.retries += 1
                    self._emit("work_retried", work_id=item.id, role=item.role, attempt=item.attempts, error=(result.error or "")[:200])
                else:
                    escalated = await self._escalate(item, result, resolved, outcome, max_replans)
                    if not escalated:
                        item.transition(WorkState.BLOCKED, note=result.error or "retries exhausted")
                        self._emit("work_blocked", work_id=item.id, role=item.role, error=(result.error or "")[:200])
                        outcome.blockers.append(f"{get_role(item.role).label}: {result.error or result.summary}")

            for blocked in resolved.blocked_by_failure():
                blocked.transition(WorkState.BLOCKED, note="a dependency did not succeed")
                self._emit("dependency_failed", work_id=blocked.id, role=blocked.role)
                if blocked.required:
                    outcome.blockers.append(f"{get_role(blocked.role).label} blocked: an earlier worker did not succeed")

        # -- finish --------------------------------------------------------- #
        succeeded = outcome.succeeded()
        failed = outcome.failed()
        if succeeded:
            outcome.merge = merge_results(
                [outcome.results[item.id] for item in succeeded if item.id in outcome.results],
                strategy=str(self._cfg("merge_strategy", "owner_partition")),
            )
            self._emit("merge_finished", strategy=outcome.merge.strategy, conflicts=len(outcome.merge.conflicts), ok=outcome.merge.ok)
            if outcome.merge.conflicts:
                self._emit("merge_conflict", conflicts=outcome.merge.conflicts[:4])
                outcome.blockers.extend(
                    f"merge conflict on {conflict['path']} ({', '.join(conflict['roles'])})" for conflict in outcome.merge.conflicts[:3]
                )

        required_failed = [item for item in failed if item.required]
        if outcome.status != "cancelled":
            if not failed and succeeded:
                outcome.status = "completed"
                outcome.ok = True
            elif required_failed:
                outcome.status = "blocked" if any(item.state is WorkState.BLOCKED for item in required_failed) else "failed"
            elif succeeded:
                outcome.status = "partial"
                outcome.ok = bool(self._cfg("accept_partial", False))
            else:
                outcome.status = "failed"

        outcome.duration_s = round(time.time() - started, 2)
        outcome.finished_at = time.time()
        outcome.summary = self._summarise(outcome)
        self._emit(
            "run_finished",
            status=outcome.status,
            ok=outcome.ok,
            succeeded=len(succeeded),
            failed=len(failed),
            retries=outcome.retries,
            escalations=outcome.escalations,
            duration_s=outcome.duration_s,
        )
        return outcome

    async def _execute(self, item: WorkItem, outcome: OrchestrationOutcome) -> WorkResult:
        role = get_role(item.role)
        graph = outcome.graph
        worker_context = WorkerContext(
            project_root=self.context.project_root,
            task=self.context.task,
            blueprint_text=self.context.blueprint_text,
            repo_map=self.context.repo_map,
            conventions=list(self.context.conventions),
            skill_prompt=self.context.skill_prompt,
            policy_summary=self.context.policy_summary,
            previous_results={
                get_role(dependency.role).label: result
                for dependency, result in (
                    (graph.by_id(dep_id), outcome.results.get(dep_id)) for dep_id in item.depends_on
                )
                if dependency is not None and result is not None
            },
            settings=self.context.settings,
        )
        # The worker's own events (start, boundary breaches) go to the same ledger
        # as the conductor's, so the audit trail has no gaps.
        worker = Worker(
            role,
            executor=self.executor,
            context=worker_context,
            on_event=lambda kind, payload: self._emit(
                kind,
                work_id=payload.get("work_id", ""),
                role=payload.get("role", ""),
                **{key: value for key, value in payload.items() if key not in {"work_id", "role"}},
                **({"summary": result.summary[:200]} if kind == "worker_finished" and (result := item.result) else {}),
            ),
        )
        result = await worker.run(item)
        # ``worker_finished`` is emitted by the worker itself through the ledger
        # bridge above; emitting it here too would double every entry.
        self.ledger.record(
            "note",
            work_id=item.id,
            role=item.role,
            steps=result.steps_used,
            tokens=result.tokens_used,
            duration_s=result.duration_s,
        )
        if not result.ok:
            self._emit("work_failed", work_id=item.id, role=item.role, error=(result.error or "")[:200], attempt=item.attempts)
        return result

    async def _verify(self, item: WorkItem, result: WorkResult, outcome: OrchestrationOutcome) -> bool:
        role = get_role(item.role)
        gates = list(role.verify_gates)
        self._emit("verify_started", work_id=item.id, role=item.role, gates=gates)
        try:
            report = self.verifier(gates, item)  # type: ignore[misc]
            if inspect.isawaitable(report):
                report = await report
        except Exception as exc:  # noqa: BLE001
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        passed = bool(report.get("ok", True)) if isinstance(report, Mapping) else bool(report)
        outcome.verification[item.id] = dict(report) if isinstance(report, Mapping) else {"ok": passed}
        self._emit(
            "verify_finished",
            work_id=item.id,
            role=item.role,
            ok=passed,
            gates=gates,
            detail=(report.get("detail") if isinstance(report, Mapping) else "") or "",
        )
        if passed:
            return True
        detail = (report.get("detail") if isinstance(report, Mapping) else "") or "a required gate is red"
        result.ok = False
        result.error = f"verification failed: {detail}"
        if item.attempts_left and item.attempts <= int(self._cfg("max_retries_per_worker", 2)):
            item.context = "\n".join(part for part in (item.context, f"GATE FAILURE TO FIX: {detail}") if part)
            # The worker already recorded success; verification disagreed.  Reopening
            # is the only legal way back from SUCCEEDED, and it keeps the reason.
            item.reopen(f"gate red: {detail}"[:180])
            outcome.retries += 1
            self._emit("work_retried", work_id=item.id, role=item.role, reason="gate red", detail=str(detail)[:200])
        else:
            item.reopen(f"gate red: {detail}"[:180], to=WorkState.BLOCKED)
            self._emit("work_blocked", work_id=item.id, role=item.role, reason=str(detail)[:200])
            outcome.blockers.append(f"{role.label} left a gate red: {detail}")
        return False

    async def _escalate(
        self,
        item: WorkItem,
        result: WorkResult,
        graph: WorkGraph,
        outcome: OrchestrationOutcome,
        max_replans: int,
    ) -> bool:
        """Hand a repeated failure to the planner for a revision.

        Escalation is bounded (``max_replans``) and only happens when the config
        allows it, because an agent that replans forever is an agent that never
        finishes.  Returns True when the item was re-queued behind a new plan.
        """

        if not bool(self._cfg("escalate_to_planner", True)):
            return False
        if self._replans >= max_replans:
            return False
        if not has_role("planner") or "planner" not in self.router._allowed_roles(()):
            return False

        self._replans += 1
        outcome.escalations += 1
        self._emit(
            "escalation",
            work_id=item.id,
            role=item.role,
            reason=(result.error or result.summary)[:200],
            replan=self._replans,
        )
        revision = WorkItem(
            role="planner",
            task=(
                f"A worker failed and needs a revised approach. Original task: {item.task[:200]}. "
                f"Failure: {(result.error or result.summary)[:300]}. Produce a smaller, safer decomposition."
            ),
            context=item.context[:800],
            priority=10,
            required=True,
            max_steps=int(self._cfg("worker_max_steps", 24)),
        )
        graph.items.append(revision)
        self._emit("replan", work_id=revision.id, role="planner", target=item.id)
        revision.transition(WorkState.QUEUED, note="escalation")
        revision_result = await self._execute(revision, outcome)
        outcome.results[revision.id] = revision_result
        if not revision_result.ok:
            revision.transition(WorkState.BLOCKED, note="escalation planning failed")
            return False
        revision.transition(WorkState.SUCCEEDED, note="revised plan produced")
        item.context = "\n".join(
            part for part in (item.context, f"REVISED GUIDANCE FROM PLANNER: {revision_result.summary[:600]}") if part
        )
        item.depends_on = [dependency for dependency in item.depends_on if dependency != revision.id]
        item.transition(WorkState.QUEUED, note="re-queued after escalation")
        return True

    def _summarise(self, outcome: OrchestrationOutcome) -> str:
        succeeded = len(outcome.succeeded())
        total = len(outcome.graph.items)
        roles = ", ".join(
            f"{get_role(item.role).label}" for item in outcome.graph.items if item.state is WorkState.SUCCEEDED
        )
        head = f"{outcome.pattern} orchestration: {succeeded}/{total} workers succeeded"
        if outcome.retries:
            head += f", {outcome.retries} retries"
        if outcome.escalations:
            head += f", {outcome.escalations} escalations"
        if roles:
            head += f" ({roles})"
        if outcome.unverified:
            head += f"; {len(outcome.unverified)} workers' gates were not verified (no verifier supplied)"
        if outcome.blockers:
            head += f". Blocked by: {outcome.blockers[0][:120]}"
        return head


# --------------------------------------------------------------------------- #
# convenience entry point
# --------------------------------------------------------------------------- #
async def orchestrate(
    task: str,
    *,
    config: Any = None,
    executor: WorkExecutor | None = None,
    blueprint: Blueprint | None = None,
    milestones: Sequence[Mapping[str, Any]] = (),
    project_root: str = ".",
    ledger_path: Any = None,
    verifier: Callable[[Sequence[str], WorkItem], Any] | None = None,
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    run_id: str = "",
    dry_run: bool = False,
) -> OrchestrationOutcome:
    """One-call orchestration used by ``ua orchestrate run`` and the runner.

    The audit trail is on by default and lands at
    ``<project_root>/.unknown/orchestration/<run-id>.jsonl`` — the promise the
    ledger module makes is that "why did the agent do that?" always has an answer
    on disk, and a default of *nowhere* would quietly break it.  ``[orchestration]
    ledger = false`` keeps events in memory only.

    A dry run never writes a file: nothing was delegated, so creating an audit
    record would pollute ``ua orchestrate list`` with runs that did not happen.
    It still validates the graph, so ``ok`` means "this plan is executable".
    """

    router = Router(config=config)
    conductor = Conductor(
        config=config,
        executor=executor or OfflineExecutor(),
        context=WorkerContext(project_root=project_root),
        verifier=verifier,
        on_event=on_event,
        router=router,
        run_id=run_id,
    )

    if dry_run:
        ledger = Ledger(enabled=False, run_id=conductor.run_id)
        graph = router.build(task, blueprint=blueprint, milestones=milestones)
        problems = graph.validate()
        outcome = OrchestrationOutcome(
            run_id=conductor.run_id,
            pattern=graph.pattern,
            graph=graph,
            status="dry-run",
            ok=not problems,
            blockers=list(problems),
            summary=(
                f"dry run: {graph.pattern} shape with {len(graph.items)} workers — {graph.rationale}"
                if not problems
                else f"dry run: {graph.pattern} shape is not executable — {'; '.join(problems[:2])}"
            ),
        )
        ledger.record("note", note="dry run: no worker executed")
        outcome.merge = None
        return outcome

    ledger_enabled = bool(getattr(config, "ledger", True) if config is not None else True)
    target: Any = ledger_path
    if target is None and ledger_enabled:
        target = ledger_path_for(project_root, conductor.run_id)
    conductor.ledger = Ledger(target, run_id=conductor.run_id, enabled=ledger_enabled)
    try:
        return await conductor.run(task, blueprint=blueprint, milestones=milestones)
    finally:
        conductor.ledger.close()
