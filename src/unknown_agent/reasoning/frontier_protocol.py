"""Pre-action reasoning protocol for frontier models.

A frontier model used as an autocomplete engine is an expensive autocomplete
engine.  The value is in what it can work out *before* it acts — and the only
way to get that reliably is to make the reflection a **typed artefact the
runtime validates**, not a polite request in a system prompt.

That is what this module is.  It implements a two-phase loop around every
consequential action:

**Phase 1 — Pre-Execution Thought Graph.**  Before a destructive or structural
tool call, the model must emit a :class:`ThoughtGraph`: the invariants it
believes hold, the assumptions it is relying on (each marked verified or not),
the side effects it predicts, at least one genuinely different path it
considered and rejected, and a concrete rollback strategy.  The graph is a
Pydantic v2 model, so a half-written reflection fails validation instead of
sliding through.  :meth:`FrontierProtocol.adjudicate` then applies rules the
prose cannot talk its way around — a destructive action with no executable
rollback is refused, an unverified assumption on a structural change is
refused.

**Phase 2 — Structured Execution.**  Tool calls are dispatched only after a
verdict of :attr:`Verdict.PROCEED`.  When one fails, the model does not guess
again: :func:`analyse_failure` turns the traceback into a typed
:class:`FailureAnalysis`, and any repair patch is syntax-checked with
:func:`verify_patch_syntax` *before* it reaches disk.

Two supporting concerns live here because they are part of the same bargain:

* **Thinking budget by phase** (:class:`Phase`) — exhaustive for architecture,
  moderate and AST-focused for surgical edits, near-zero for deterministic test
  runs.  Spending a 48k thinking budget to run ``pytest`` is waste, not rigour.
* **Cache-segmented context** (:class:`ContextAssembler`) — long missions are
  only affordable if the stable prefix of the prompt is cached.  The assembler
  splits context into stable/semi-stable/volatile segments and emits provider
  cache breakpoints, so re-evaluating a month-old mission costs a fraction of
  the original.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from unknown_agent.core.models import RiskLevel, ToolCall
from unknown_agent.observability.logging import get_logger
from unknown_agent.reasoning import ReasoningDepth, depth_for, normalize_mode

log = get_logger("reasoning.frontier")

__all__ = [
    "Phase",
    "ActionClass",
    "Verdict",
    "Assumption",
    "Invariant",
    "SideEffect",
    "CounterfactualPath",
    "RollbackPlan",
    "ThoughtGraph",
    "Adjudication",
    "FailureAnalysis",
    "RepairHypothesis",
    "PatchVerification",
    "CacheSegment",
    "AssembledContext",
    "ContextAssembler",
    "FrontierProtocol",
    "classify_action",
    "analyse_failure",
    "verify_patch_syntax",
    "THOUGHT_GRAPH_SCHEMA",
]


# --------------------------------------------------------------------------- #
# phases and budgets
# --------------------------------------------------------------------------- #
class Phase(str, Enum):
    """Where in a mission we are — decides how much thinking is warranted."""

    #: Decomposition, architecture, acceptance criteria. Think as hard as possible.
    ARCHITECTURE = "architecture"
    #: Choosing between concrete implementations of a settled design.
    DESIGN = "design"
    #: Surgical edit to existing code. Moderate, AST-invariant focused.
    SURGICAL_EDIT = "surgical_edit"
    #: Bulk mechanical change with a known recipe.
    MECHANICAL = "mechanical"
    #: Running tests / gates. Deterministic, fast, no deliberation.
    VERIFICATION = "verification"
    #: Reading the tree to establish ground truth.
    INSPECTION = "inspection"
    #: Diagnosing a failure that already happened.
    DIAGNOSIS = "diagnosis"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize()


#: Baseline reasoning rung per phase, before role/complexity adjustment.
PHASE_MODE: dict[Phase, str] = {
    Phase.ARCHITECTURE: "ultra",
    Phase.DESIGN: "max",
    Phase.SURGICAL_EDIT: "high",
    Phase.MECHANICAL: "medium",
    Phase.VERIFICATION: "default",
    Phase.INSPECTION: "medium",
    Phase.DIAGNOSIS: "super",
}

#: Share of the ceiling each phase may spend on thinking.  Verification is
#: deliberately near-zero: a deterministic subprocess does not need deliberation.
PHASE_BUDGET_SHARE: dict[Phase, float] = {
    Phase.ARCHITECTURE: 1.00,
    Phase.DESIGN: 0.75,
    Phase.SURGICAL_EDIT: 0.45,
    Phase.MECHANICAL: 0.20,
    Phase.VERIFICATION: 0.02,
    Phase.INSPECTION: 0.25,
    Phase.DIAGNOSIS: 0.80,
}


class ActionClass(str, Enum):
    """How much damage an action can do if the reasoning behind it is wrong."""

    #: No state change. Never gated.
    READ = "read"
    #: Writes inside the project tree, recoverable from git or a backup.
    MUTATE = "mutate"
    #: Changes shape: moves/deletes files, edits build or dependency manifests.
    STRUCTURAL = "structural"
    #: Irreversible or outside the tree: rm -rf, push, publish, deploy, DB drop.
    DESTRUCTIVE = "destructive"

    @property
    def requires_thought_graph(self) -> bool:
        return self in {ActionClass.STRUCTURAL, ActionClass.DESTRUCTIVE}

    @property
    def rank(self) -> int:
        return {
            ActionClass.READ: 0,
            ActionClass.MUTATE: 1,
            ActionClass.STRUCTURAL: 2,
            ActionClass.DESTRUCTIVE: 3,
        }[self]


class Verdict(str, Enum):
    """What the runtime decided about a proposed action."""

    PROCEED = "proceed"
    #: Reflection was incomplete or unconvincing; the model must try again.
    REVISE = "revise"
    #: The action is refused outright at this confidence level.
    ABORT = "abort"


# --------------------------------------------------------------------------- #
# action classification
# --------------------------------------------------------------------------- #
#: Tools that reshape the tree even though their risk level may read as "write".
_STRUCTURAL_TOOLS = frozenset(
    {"move_path", "delete_path", "create_file", "apply_patch", "git_commit", "git_checkout"}
)
_DESTRUCTIVE_TOOLS = frozenset({"git_push", "npm_publish", "deploy", "db_migrate"})

#: Manifest and build files whose edit changes the shape of the project.
_STRUCTURAL_PATHS = re.compile(
    r"(^|/)(pyproject\.toml|setup\.py|setup\.cfg|package\.json|package-lock\.json|"
    r"pnpm-lock\.yaml|yarn\.lock|Cargo\.toml|go\.mod|requirements[^/]*\.txt|Dockerfile|"
    r"docker-compose[^/]*\.ya?ml|Makefile|tsconfig\.json|\.github/workflows/[^/]+)$",
    re.IGNORECASE,
)

#: Shell fragments that are irreversible in practice.
_DESTRUCTIVE_SHELL = (
    re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+", re.IGNORECASE),
    re.compile(r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f)", re.IGNORECASE),
    re.compile(r"\b(drop|truncate)\s+(table|database)\b", re.IGNORECASE),
    re.compile(r"\b(npm|pnpm|yarn)\s+publish\b", re.IGNORECASE),
    re.compile(r"\b(kubectl|terraform|aws|gcloud)\s+\w*(apply|destroy|delete)\b", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\bmkfs|\bdd\s+if=", re.IGNORECASE),
    re.compile(r">\s*/dev/(sd|nvme)", re.IGNORECASE),
)

_PATH_ARGS = ("path", "file", "target", "destination", "dest", "src", "source", "paths")


def _arg_paths(arguments: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for key in _PATH_ARGS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(str(item) for item in value if item)
    return found


def classify_action(
    call: ToolCall,
    *,
    risk: RiskLevel | None = None,
    project_root: str = "",
) -> ActionClass:
    """Decide how consequential a tool call is.

    Classification is intentionally pessimistic: the cost of an unnecessary
    reflection is tokens, the cost of a missed one is the user's repository.
    """

    name = (call.name or "").strip().lower()
    arguments: Mapping[str, Any] = call.arguments or {}

    if name in _DESTRUCTIVE_TOOLS:
        return ActionClass.DESTRUCTIVE

    command = ""
    for key in ("command", "cmd", "script", "shell"):
        raw = arguments.get(key)
        if isinstance(raw, str) and raw:
            command = raw
            break
    if command:
        for pattern in _DESTRUCTIVE_SHELL:
            if pattern.search(command):
                return ActionClass.DESTRUCTIVE

    paths = _arg_paths(arguments)
    for raw_path in paths:
        normalised = raw_path.replace("\\", "/")
        # Escaping the project tree is structural at best.
        if normalised.startswith("/") and project_root and not normalised.startswith(project_root):
            return ActionClass.DESTRUCTIVE
        if ".." in normalised.split("/"):
            return ActionClass.DESTRUCTIVE
        if _STRUCTURAL_PATHS.search(normalised):
            return ActionClass.STRUCTURAL

    if name in {"delete_path", "move_path"}:
        return ActionClass.STRUCTURAL
    if name in _STRUCTURAL_TOOLS:
        return ActionClass.STRUCTURAL

    if risk is not None:
        if risk == RiskLevel.READ:
            return ActionClass.READ
        if risk == RiskLevel.EXECUTE and command:
            return ActionClass.MUTATE
        if risk in {RiskLevel.WRITE, RiskLevel.EXECUTE}:
            return ActionClass.MUTATE

    read_prefixes = ("read", "list", "glob", "search", "inspect", "git_status", "git_diff", "git_log")
    if name.startswith(read_prefixes):
        return ActionClass.READ
    if name.startswith(("write", "edit", "patch", "create", "append")):
        return ActionClass.MUTATE
    return ActionClass.MUTATE


# --------------------------------------------------------------------------- #
# the thought graph (Phase 1 artefact)
# --------------------------------------------------------------------------- #
class Assumption(BaseModel):
    """Something the plan depends on that is not self-evidently true."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=8, description="What is being assumed, stated falsifiably.")
    verified: bool = Field(default=False, description="Has this been checked against the tree?")
    evidence: str = Field(
        default="",
        description="File, command, or tool output proving it. Required when verified is true.",
    )
    blast_radius: Literal["local", "module", "project", "external"] = "module"

    @model_validator(mode="after")
    def _evidence_when_verified(self) -> "Assumption":
        # A field validator is not sufficient here: Pydantic does not validate
        # an omitted default value unless validate_default is enabled. The
        # model-level invariant runs whether ``evidence`` was supplied or not.
        if self.verified and not self.evidence.strip():
            raise ValueError("a verified assumption must cite its evidence")
        return self


class Invariant(BaseModel):
    """A property of the codebase that must still hold after the action."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=8)
    #: How the invariant is checked — a gate name, a test id, or a command.
    check: str = Field(min_length=2, description="Executable check: test id, gate, or command.")
    holds_before: bool = True


class SideEffect(BaseModel):
    """A consequence the action has beyond its stated purpose."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=8)
    scope: Literal["file", "module", "project", "external"] = "module"
    reversible: bool = True
    mitigation: str = ""


class CounterfactualPath(BaseModel):
    """A genuinely different approach that was considered and rejected."""

    model_config = ConfigDict(extra="forbid")

    approach: str = Field(min_length=8)
    why_rejected: str = Field(min_length=8)
    #: What would make this the better choice after all.
    revisit_if: str = ""


class RollbackPlan(BaseModel):
    """How to undo the action if it turns out to be wrong."""

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["git_revert", "backup_restore", "inverse_patch", "none"] = "git_revert"
    #: Concrete commands or steps. Must be non-empty unless strategy is "none".
    steps: list[str] = Field(default_factory=list)
    #: Seconds to restore. Long recovery on a destructive action is a red flag.
    estimated_seconds: int = Field(default=30, ge=0)
    verified_possible: bool = False

    @field_validator("steps")
    @classmethod
    def _steps_present(cls, value: list[str], info: Any) -> list[str]:
        if info.data.get("strategy") != "none" and not [item for item in value if item.strip()]:
            raise ValueError("a rollback strategy other than 'none' needs at least one step")
        return value


class ThoughtGraph(BaseModel):
    """The typed reflection a model must produce before a consequential act."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=8, description="What this action is meant to achieve.")
    action_summary: str = Field(min_length=8, description="What will concretely happen.")
    repo_state: str = Field(
        default="",
        description="Relevant current state of the tree, as observed — not assumed.",
    )
    invariants: list[Invariant] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    side_effects: list[SideEffect] = Field(default_factory=list)
    counterfactuals: list[CounterfactualPath] = Field(default_factory=list)
    rollback: RollbackPlan = Field(default_factory=RollbackPlan)
    #: 0..1. Honest self-assessment; low confidence plus high risk triggers ABORT.
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    open_questions: list[str] = Field(default_factory=list)

    # -- derived signals ---------------------------------------------------
    @property
    def unverified_assumptions(self) -> list[Assumption]:
        return [item for item in self.assumptions if not item.verified]

    @property
    def irreversible_effects(self) -> list[SideEffect]:
        return [item for item in self.side_effects if not item.reversible]

    @property
    def broken_invariants(self) -> list[Invariant]:
        return [item for item in self.invariants if not item.holds_before]

    def fingerprint(self) -> str:
        """Stable hash of the reasoning, used to detect a model going in circles."""

        payload = json.dumps(
            {
                "goal": self.goal.strip().lower(),
                "action": self.action_summary.strip().lower(),
                "rollback": self.rollback.strategy,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def digest(self, *, max_chars: int = 1200) -> str:
        """Human-readable condensation for the TUI's collapsible thinking block."""

        lines = [f"goal: {self.goal}", f"action: {self.action_summary}"]
        if self.invariants:
            named = "; ".join(f"{item.description} [{item.check}]" for item in self.invariants[:3])
            lines.append(f"invariants ({len(self.invariants)}): {named}")
        unverified = self.unverified_assumptions
        if unverified:
            joined = "; ".join(item.statement for item in unverified[:3])
            lines.append(f"unverified ({len(unverified)}): {joined}")
        if self.counterfactuals:
            first = self.counterfactuals[0]
            lines.append(f"rejected: {first.approach} — {first.why_rejected}")
        lines.append(f"rollback: {self.rollback.strategy} ({len(self.rollback.steps)} step(s))")
        lines.append(f"confidence: {self.confidence:.0%}")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


#: JSON Schema handed to the model so it emits a parseable graph first time.
THOUGHT_GRAPH_SCHEMA: dict[str, Any] = ThoughtGraph.model_json_schema()


@dataclass(slots=True)
class Adjudication:
    """The runtime's ruling on a proposed action."""

    verdict: Verdict
    action_class: ActionClass
    reasons: list[str] = field(default_factory=list)
    graph: ThoughtGraph | None = None
    #: Text to feed back to the model when the verdict is REVISE.
    feedback: str = ""
    required_before_retry: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.PROCEED

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "action_class": self.action_class.value,
            "reasons": list(self.reasons),
            "feedback": self.feedback,
            "required_before_retry": list(self.required_before_retry),
            "graph": self.graph.model_dump() if self.graph else None,
        }


# --------------------------------------------------------------------------- #
# failure analysis (Phase 2)
# --------------------------------------------------------------------------- #
_TRACEBACK_FRAME = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>[^\n]+)'
)
_EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit))\s*:\s*(?P<msg>.*)$")
_PYTEST_FAIL = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):\s+(?P<msg>.+)$")


@dataclass(slots=True)
class FailureAnalysis:
    """A traceback turned into something a model can reason about precisely."""

    exception_type: str = ""
    message: str = ""
    #: (file, line, function), innermost last — same order Python prints them.
    frames: list[tuple[str, int, str]] = field(default_factory=list)
    #: Frames inside the project, which is where the fix almost always belongs.
    project_frames: list[tuple[str, int, str]] = field(default_factory=list)
    raw: str = ""
    exit_code: int | None = None

    @property
    def culprit(self) -> tuple[str, int, str] | None:
        """Innermost project frame — the best single guess at where to look."""

        if self.project_frames:
            return self.project_frames[-1]
        return self.frames[-1] if self.frames else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exception_type": self.exception_type,
            "message": self.message,
            "frames": [list(frame) for frame in self.frames],
            "project_frames": [list(frame) for frame in self.project_frames],
            "culprit": list(self.culprit) if self.culprit else None,
            "exit_code": self.exit_code,
        }

    def digest(self) -> str:
        spot = self.culprit
        where = f"{spot[0]}:{spot[1]} in {spot[2]}" if spot else "unknown location"
        return f"{self.exception_type or 'failure'}: {self.message or 'no message'} @ {where}"


def analyse_failure(
    output: str,
    *,
    project_root: str = "",
    exit_code: int | None = None,
) -> FailureAnalysis:
    """Parse a traceback or test failure into structured form.

    Works on CPython tracebacks and on pytest's ``file:line: message`` summary
    lines, which between them cover the overwhelming majority of what a coding
    agent actually has to diagnose.
    """

    text = output or ""
    analysis = FailureAnalysis(raw=text[-8000:], exit_code=exit_code)

    for match in _TRACEBACK_FRAME.finditer(text):
        frame = (match.group("file"), int(match.group("line")), match.group("func").strip())
        analysis.frames.append(frame)

    # The exception line is the last line matching the pattern, scanning back.
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        hit = _EXC_LINE.match(stripped)
        if hit:
            analysis.exception_type = hit.group("type")
            analysis.message = hit.group("msg").strip()
            break

    if not analysis.frames:
        for line in text.splitlines():
            hit = _PYTEST_FAIL.match(line.strip())
            if hit and ("/" in hit.group("file") or hit.group("file").endswith(".py")):
                analysis.frames.append((hit.group("file"), int(hit.group("line")), "<pytest>"))
                if not analysis.message:
                    analysis.message = hit.group("msg").strip()

    root = (project_root or "").rstrip("/")
    for frame in analysis.frames:
        path = frame[0].replace("\\", "/")
        in_project = bool(root) and path.startswith(root)
        vendored = "/site-packages/" in path or "/lib/python" in path or path.startswith("<")
        if (in_project or not root) and not vendored:
            analysis.project_frames.append(frame)
    return analysis


class RepairHypothesis(BaseModel):
    """A typed repair proposal, checked before it is allowed near the disk."""

    model_config = ConfigDict(extra="forbid")

    diagnosis: str = Field(min_length=8, description="Why the failure happened, mechanically.")
    target_file: str = Field(min_length=1)
    #: What will change, in one line.
    change_summary: str = Field(min_length=8)
    #: Full replacement content, or empty when using an inverse patch elsewhere.
    new_content: str = ""
    #: Falsifiable prediction: what turns green if this is right.
    expected_signal: str = Field(min_length=4, description="Test or gate expected to pass after.")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


@dataclass(slots=True)
class PatchVerification:
    """Result of syntax-checking a proposed patch before writing it."""

    ok: bool
    language: str
    error: str = ""
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "language": self.language, "error": self.error, "line": self.line}


def verify_patch_syntax(path: str, content: str) -> PatchVerification:
    """Parse candidate file content before it is persisted.

    Writing a syntactically broken file costs the next worker a whole step to
    discover and another to fix.  Catching it here costs microseconds.
    """

    lowered = path.lower()
    if lowered.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return PatchVerification(False, "python", f"{exc.msg}", exc.lineno)
        return PatchVerification(True, "python")
    if lowered.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return PatchVerification(False, "json", exc.msg, exc.lineno)
        return PatchVerification(True, "json")
    if lowered.endswith((".yaml", ".yml")):
        try:
            import yaml

            yaml.safe_load(content)
        except Exception as exc:  # noqa: BLE001 - yaml raises a family of errors
            detail = str(exc).splitlines()[0] if str(exc) else "invalid YAML"
            return PatchVerification(False, "yaml", detail)
        return PatchVerification(True, "yaml")
    if lowered.endswith(".toml"):
        try:
            import tomllib

            tomllib.loads(content)
        except Exception as exc:  # noqa: BLE001 - tomllib raises TOMLDecodeError
            detail = str(exc).splitlines()[0] if str(exc) else "invalid TOML"
            return PatchVerification(False, "toml", detail)
        return PatchVerification(True, "toml")
    # Unknown language: a balanced-delimiter check still catches truncated writes,
    # which is the most common way a generated file arrives broken.
    if content.count("{") != content.count("}") or content.count("[") != content.count("]"):
        return PatchVerification(
            False, "text", "unbalanced braces or brackets — content looks truncated"
        )
    return PatchVerification(True, "text")


# --------------------------------------------------------------------------- #
# cache-segmented context
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CacheSegment:
    """One block of prompt context with a declared volatility."""

    name: str
    content: str
    #: ``stable`` survives the whole mission, ``volatile`` changes every turn.
    volatility: Literal["stable", "semi", "volatile"] = "semi"
    #: Emit a provider cache breakpoint after this segment.
    cache_breakpoint: bool = False

    @property
    def approx_tokens(self) -> int:
        # ~4 chars per token is accurate enough for budgeting decisions.
        return max(1, len(self.content) // 4)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class AssembledContext:
    """Context ordered so the cacheable prefix is genuinely stable."""

    segments: list[CacheSegment]
    text: str
    cached_prefix_tokens: int
    volatile_tokens: int
    breakpoints: int

    @property
    def total_tokens(self) -> int:
        return self.cached_prefix_tokens + self.volatile_tokens

    @property
    def cache_ratio(self) -> float:
        total = self.total_tokens
        return self.cached_prefix_tokens / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [
                {
                    "name": segment.name,
                    "volatility": segment.volatility,
                    "tokens": segment.approx_tokens,
                    "fingerprint": segment.fingerprint(),
                    "cache_breakpoint": segment.cache_breakpoint,
                }
                for segment in self.segments
            ],
            "cached_prefix_tokens": self.cached_prefix_tokens,
            "volatile_tokens": self.volatile_tokens,
            "cache_ratio": round(self.cache_ratio, 3),
            "breakpoints": self.breakpoints,
        }


class ContextAssembler:
    """Order context by volatility so prompt caching actually pays.

    Providers cache a *prefix*.  One volatile line near the top invalidates
    everything after it, which is how teams end up paying full price for a
    200k-token context every turn.  This assembler sorts stable content first,
    inserts breakpoints at the boundaries, and reports the ratio so the UI can
    show whether caching is working.
    """

    _ORDER: dict[str, int] = {"stable": 0, "semi": 1, "volatile": 2}

    def __init__(self, *, max_breakpoints: int = 4) -> None:
        # Anthropic allows four cache breakpoints; staying at or under the
        # smallest common limit keeps one assembler valid across vendors.
        self._max_breakpoints = max(1, max_breakpoints)
        self._segments: list[CacheSegment] = []

    def add(
        self,
        name: str,
        content: str,
        *,
        volatility: Literal["stable", "semi", "volatile"] = "semi",
    ) -> "ContextAssembler":
        if content and content.strip():
            self._segments.append(CacheSegment(name=name, content=content, volatility=volatility))
        return self

    def assemble(self) -> AssembledContext:
        ordered = sorted(
            self._segments,
            key=lambda segment: (self._ORDER.get(segment.volatility, 1), segment.name),
        )
        cached_tokens = 0
        volatile_tokens = 0
        breakpoints = 0
        previous: str | None = None

        for index, segment in enumerate(ordered):
            segment.cache_breakpoint = False
            if segment.volatility == "volatile":
                volatile_tokens += segment.approx_tokens
            else:
                cached_tokens += segment.approx_tokens
            # Break at each volatility boundary, while budget allows.
            is_boundary = previous is not None and segment.volatility != previous
            last_cacheable = (
                index + 1 < len(ordered)
                and segment.volatility != "volatile"
                and ordered[index + 1].volatility == "volatile"
            )
            if (is_boundary or last_cacheable) and breakpoints < self._max_breakpoints:
                if index > 0 and ordered[index - 1].volatility != "volatile":
                    ordered[index - 1].cache_breakpoint = True
                    breakpoints += 1
            previous = segment.volatility

        text = "\n\n".join(
            f"<context name=\"{segment.name}\" volatility=\"{segment.volatility}\">\n"
            f"{segment.content.strip()}\n</context>"
            for segment in ordered
        )
        return AssembledContext(
            segments=ordered,
            text=text,
            cached_prefix_tokens=cached_tokens,
            volatile_tokens=volatile_tokens,
            breakpoints=breakpoints,
        )


# --------------------------------------------------------------------------- #
# the protocol
# --------------------------------------------------------------------------- #
_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n(?P<body>\{.*?\})\s*\n```", re.DOTALL)
_THINK_BLOCK = re.compile(
    r"<(?P<tag>thought_graph|thinking|reasoning)>(?P<body>.*?)</(?P=tag)>", re.DOTALL | re.IGNORECASE
)


class FrontierProtocol:
    """Runs the two-phase loop and holds the policy that gates actions.

    ``strictness`` scales how much reflection is demanded:

    * ``0`` — gate destructive actions only.
    * ``1`` — gate destructive and structural actions (the default).
    * ``2`` — gate every mutation, for high-stakes or unattended missions.
    """

    def __init__(
        self,
        *,
        thinking_ceiling: int = 32_000,
        strictness: int = 1,
        project_root: str = "",
        min_confidence_destructive: float = 0.7,
    ) -> None:
        self._ceiling = max(0, thinking_ceiling)
        self._strictness = max(0, min(2, strictness))
        self._project_root = (project_root or "").rstrip("/")
        self._min_confidence = min_confidence_destructive
        self._adjudications: list[Adjudication] = []
        self._fingerprints: dict[str, int] = {}

    # -- budgets -----------------------------------------------------------
    def depth_for_phase(self, phase: Phase, *, override_mode: str | None = None) -> ReasoningDepth:
        return depth_for(normalize_mode(override_mode) if override_mode else PHASE_MODE[phase])

    def thinking_budget(
        self,
        phase: Phase,
        *,
        override_mode: str | None = None,
        max_output_tokens: int = 0,
    ) -> int:
        """Tokens of internal thinking this phase is allowed to spend."""

        depth = self.depth_for_phase(phase, override_mode=override_mode)
        share = PHASE_BUDGET_SHARE[phase]
        wanted = int(round(min(depth.budget_from(self._ceiling), self._ceiling * share)))
        if max_output_tokens > 0:
            wanted = min(wanted, max(0, max_output_tokens - 1024))
        return max(0, wanted)

    # -- phase 1 -----------------------------------------------------------
    def requires_reflection(self, action: ActionClass) -> bool:
        if self._strictness >= 2:
            return action.rank >= ActionClass.MUTATE.rank
        if self._strictness == 1:
            return action.requires_thought_graph
        return action is ActionClass.DESTRUCTIVE

    def build_reflection_prompt(
        self,
        call: ToolCall,
        action: ActionClass,
        *,
        repo_state: str = "",
        prior_failures: Sequence[str] = (),
    ) -> str:
        """The instruction that elicits a valid :class:`ThoughtGraph`."""

        arguments = json.dumps(call.arguments or {}, indent=2, ensure_ascii=False, default=str)
        demands = [
            "At least one invariant with an *executable* check (a gate name, test id, or command).",
            "Every assumption you are relying on, each marked verified true/false; "
            "a verified assumption must cite the file or command that proves it.",
            "At least one genuinely different approach you considered and why you rejected it.",
        ]
        if action is ActionClass.DESTRUCTIVE:
            demands.append(
                "A rollback plan with concrete steps and verified_possible=true. "
                "An irreversible action without a proven rollback will be refused."
            )
            demands.append("An honest confidence value; below 0.7 this action will be refused.")
        else:
            demands.append("A rollback plan with concrete steps.")

        failure_note = ""
        if prior_failures:
            joined = "\n".join(f"  - {item}" for item in prior_failures[-3:])
            failure_note = (
                "\nPrevious attempts at this action failed:\n"
                f"{joined}\n"
                "Do not repeat a rejected approach. Address the stated reason directly.\n"
            )

        return (
            f"You are about to perform a {action.value.upper()} action. "
            "Before it executes, produce a pre-execution thought graph.\n\n"
            f"Tool: {call.name}\nArguments:\n{arguments}\n\n"
            f"{'Observed repository state:' + chr(10) + repo_state + chr(10) if repo_state else ''}"
            f"{failure_note}\n"
            "Requirements:\n"
            + "\n".join(f"  {index}. {item}" for index, item in enumerate(demands, 1))
            + "\n\nRespond with a single JSON object inside a ```json fence, matching this schema:\n"
            + json.dumps(THOUGHT_GRAPH_SCHEMA, indent=2)[:4000]
            + "\n\nReason about the repository as it actually is. Do not assert anything you have "
            "not observed; mark it as an unverified assumption instead."
        )

    @staticmethod
    def parse_thought_graph(text: str) -> ThoughtGraph | str:
        """Extract and validate a graph, or return the reason it failed.

        Returning the error text rather than raising is deliberate: the caller
        feeds it straight back to the model as the REVISE feedback.
        """

        if not text or not text.strip():
            return "no reflection was produced"

        candidates: list[str] = []
        fenced = _JSON_BLOCK.search(text)
        if fenced:
            candidates.append(fenced.group("body"))
        tagged = _THINK_BLOCK.search(text)
        if tagged:
            inner = _JSON_BLOCK.search(tagged.group("body"))
            candidates.append(inner.group("body") if inner else tagged.group("body"))
        # Bare object: take the outermost balanced braces.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

        last_error = "no JSON object found in the reflection"
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_error = f"reflection is not valid JSON: {exc.msg} (line {exc.lineno})"
                continue
            if not isinstance(payload, dict):
                last_error = "reflection JSON must be an object"
                continue
            try:
                return ThoughtGraph.model_validate(payload)
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()[:6]
                )
                last_error = f"reflection does not satisfy the schema — {problems}"
        return last_error

    def adjudicate(
        self,
        call: ToolCall,
        graph: ThoughtGraph | str | None,
        *,
        action: ActionClass | None = None,
        risk: RiskLevel | None = None,
    ) -> Adjudication:
        """Apply the rules prose cannot argue with."""

        resolved = action or classify_action(call, risk=risk, project_root=self._project_root)

        if not self.requires_reflection(resolved):
            ruling = Adjudication(
                verdict=Verdict.PROCEED,
                action_class=resolved,
                reasons=["no reflection required"],
            )
            self._adjudications.append(ruling)
            return ruling

        if graph is None or isinstance(graph, str):
            reason = graph or "reflection missing"
            ruling = Adjudication(
                verdict=Verdict.REVISE,
                action_class=resolved,
                reasons=[reason],
                feedback=f"The reflection was rejected: {reason}. Produce a valid thought graph.",
                required_before_retry=["valid ThoughtGraph JSON"],
            )
            self._adjudications.append(ruling)
            return ruling

        reasons: list[str] = []
        required: list[str] = []
        verdict = Verdict.PROCEED

        # Circling detection: the same reasoning proposed repeatedly is not
        # reasoning, it is a loop.
        mark = graph.fingerprint()
        self._fingerprints[mark] = self._fingerprints.get(mark, 0) + 1
        if self._fingerprints[mark] > 2:
            seen = self._fingerprints[mark]
            reasons.append(f"identical reasoning proposed {seen} times — the approach is stuck")
            verdict = Verdict.ABORT

        if not graph.invariants:
            reasons.append("no invariant was named, so nothing would detect a regression")
            required.append("at least one invariant with an executable check")
            verdict = Verdict.REVISE
        elif not any(item.check.strip() for item in graph.invariants):
            reasons.append("invariants have no executable check")
            required.append("an executable check per invariant")
            verdict = Verdict.REVISE

        if not graph.counterfactuals:
            reasons.append("no alternative approach was considered")
            required.append("one rejected alternative with a reason")
            verdict = Verdict.REVISE if verdict is Verdict.PROCEED else verdict

        if resolved is ActionClass.DESTRUCTIVE:
            if graph.rollback.strategy == "none" or not graph.rollback.steps:
                reasons.append("irreversible action proposed with no rollback plan")
                verdict = Verdict.ABORT
            elif not graph.rollback.verified_possible:
                reasons.append("rollback plan is asserted but not verified as possible")
                required.append("verify the rollback is executable, then set verified_possible")
                verdict = Verdict.REVISE if verdict is not Verdict.ABORT else verdict
            if graph.confidence < self._min_confidence:
                reasons.append(
                    f"confidence {graph.confidence:.0%} is below the "
                    f"{self._min_confidence:.0%} floor for irreversible actions"
                )
                verdict = Verdict.ABORT

        if resolved in {ActionClass.STRUCTURAL, ActionClass.DESTRUCTIVE}:
            wide = {"project", "external"}
            risky = [i for i in graph.unverified_assumptions if i.blast_radius in wide]
            if risky:
                reasons.append(
                    "unverified assumption with project-wide blast radius: " + risky[0].statement
                )
                required.append("verify the assumption against the tree, or reduce its scope")
                verdict = Verdict.REVISE if verdict is not Verdict.ABORT else verdict

        broken = graph.broken_invariants
        if broken:
            reasons.append(
                f"{len(broken)} invariant(s) already fail before the change — fix the baseline first"
            )
            verdict = Verdict.ABORT

        irreversible = graph.irreversible_effects
        if irreversible and not any(item.mitigation.strip() for item in irreversible):
            reasons.append("irreversible side effect declared with no mitigation")
            required.append("a mitigation for each irreversible side effect")
            verdict = Verdict.REVISE if verdict is not Verdict.ABORT else verdict

        if verdict is Verdict.PROCEED and not reasons:
            reasons.append(
                f"{len(graph.invariants)} invariant(s), "
                f"{len(graph.counterfactuals)} alternative(s) considered, "
                f"rollback via {graph.rollback.strategy}, confidence {graph.confidence:.0%}"
            )

        feedback = ""
        if verdict is Verdict.REVISE:
            feedback = (
                "The action was held back. Fix these before retrying:\n"
                + "\n".join(f"  - {item}" for item in required)
                + "\nReasons: "
                + "; ".join(reasons)
            )
        elif verdict is Verdict.ABORT:
            feedback = "The action is refused: " + "; ".join(reasons)

        ruling = Adjudication(
            verdict=verdict,
            action_class=resolved,
            reasons=reasons,
            graph=graph,
            feedback=feedback,
            required_before_retry=required,
        )
        self._adjudications.append(ruling)
        log.debug("frontier: %s %s -> %s", call.name, resolved.value, verdict.value)
        return ruling

    # -- phase 2 -----------------------------------------------------------
    def diagnose(
        self,
        output: str,
        *,
        exit_code: int | None = None,
    ) -> FailureAnalysis:
        return analyse_failure(output, project_root=self._project_root, exit_code=exit_code)

    def build_repair_prompt(self, analysis: FailureAnalysis, *, source_excerpt: str = "") -> str:
        """Ask for a typed repair hypothesis rather than a blind retry."""

        spot = analysis.culprit
        location = f"{spot[0]}:{spot[1]} in {spot[2]}" if spot else "an unidentified location"
        excerpt = ""
        if source_excerpt:
            excerpt = f"\nSource around the failure:\n```\n{source_excerpt.strip()}\n```\n"
        return (
            "A tool call failed. Do not retry blindly.\n\n"
            f"Exception: {analysis.exception_type or 'unknown'}\n"
            f"Message: {analysis.message or 'none'}\n"
            f"Innermost project frame: {location}\n"
            f"Exit code: {analysis.exit_code if analysis.exit_code is not None else 'n/a'}\n"
            f"\nTraceback tail:\n```\n{analysis.raw[-2500:]}\n```\n"
            f"{excerpt}\n"
            "Work out mechanically why this happened, then respond with one JSON object in a "
            "```json fence matching this schema:\n"
            + json.dumps(RepairHypothesis.model_json_schema(), indent=2)[:2500]
            + "\n\nThe expected_signal must be a specific test or gate that is currently red and "
            "would turn green. If you cannot name one, you do not yet understand the failure."
        )

    @staticmethod
    def parse_repair(text: str) -> RepairHypothesis | str:
        fenced = _JSON_BLOCK.search(text or "")
        raw = fenced.group("body") if fenced else ""
        if not raw:
            start, end = (text or "").find("{"), (text or "").rfind("}")
            raw = text[start : end + 1] if start != -1 and end > start else ""
        if not raw:
            return "no repair hypothesis was produced"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"repair hypothesis is not valid JSON: {exc.msg}"
        try:
            return RepairHypothesis.model_validate(payload)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:5]
            )
            return f"repair hypothesis does not satisfy the schema — {problems}"

    @staticmethod
    def vet_repair(hypothesis: RepairHypothesis) -> PatchVerification:
        """Syntax-check a repair before it is written. Phase 2's last gate."""

        if not hypothesis.new_content:
            return PatchVerification(True, "none", "no content to verify")
        return verify_patch_syntax(hypothesis.target_file, hypothesis.new_content)

    # -- introspection -----------------------------------------------------
    @property
    def adjudications(self) -> list[Adjudication]:
        return list(self._adjudications)

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for ruling in self._adjudications:
            counts[ruling.verdict.value] = counts.get(ruling.verdict.value, 0) + 1
        return {
            "total": len(self._adjudications),
            "verdicts": counts,
            "strictness": self._strictness,
            "thinking_ceiling": self._ceiling,
            "repeated_reasoning": {mark: n for mark, n in self._fingerprints.items() if n > 1},
        }

