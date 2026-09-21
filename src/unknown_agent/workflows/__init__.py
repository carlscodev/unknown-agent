"""Contextual workflows: guided mini-tools the agent creates when a task needs one.

A plain prompt is the wrong interface for most real work.  "Add payments" is not
one instruction — it is a sequence of decisions (provider, mode, failure handling,
webhook verification, idempotency, test doubles) that a human expert would walk
through in order, and that an agent silently guesses if nobody asks.

This module gives Unknown Agent a **workflow layer** between "one prompt" and
"full orchestration":

* :data:`LIBRARY` — hand-authored guided workflows for the recurring shapes of
  software work (new product, feature, bug, refactor, UI polish, integration,
  release, audit).
* :func:`detect` — decide whether the current task has a workflow shape at all.
* :func:`generate` — *synthesise* a workflow for this specific blueprint: the
  library shape, parameterised by the user's answers, plus extra steps the
  answers imply (payments → webhook verification; UI → browser gate; handover →
  docs milestone).  Specs are persisted to ``.unknown/workflows/`` so a generated
  tool becomes a reusable project asset.
* :func:`run_workflow` — execute a spec through the same :class:`AskUI` protocol
  the planning panels use, so it renders identically in the TUI, in Rich inline,
  and headless in CI.

A workflow never touches the filesystem itself.  ``tool`` steps become
:class:`WorkflowToolCall` records that the caller executes through the real tool
registry, which means policy, permissions, dry-run and audit all still apply.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "WorkflowStep",
    "WorkflowSpec",
    "WorkflowToolCall",
    "WorkflowResult",
    "WorkflowMatch",
    "LIBRARY",
    "WORKFLOW_TRIGGERS",
    "library_payload",
    "get_workflow",
    "detect",
    "generate",
    "run_workflow",
    "validate_spec",
    "spec_to_yaml",
    "spec_from_dict",
]


# --------------------------------------------------------------------------- #
# spec model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class WorkflowStep:
    """One step of a guided workflow."""

    id: str
    kind: str  # ask | confirm | tool | gate | note | generate | decide
    title: str
    description: str = ""
    #: ``ask``/``decide``: a planning axis id, or inline options when None.
    target: str | None = None
    options: list[tuple[str, str]] = field(default_factory=list)
    default: str | None = None
    #: ``tool``: the tool to call and its arguments (may contain {placeholders}).
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    #: ``gate``: which validation gate must pass.
    gate: str | None = None
    #: Steps that must be complete first.
    depends_on: list[str] = field(default_factory=list)
    #: Only include this step when the blueprint says so.
    when: dict[str, tuple[str, ...]] = field(default_factory=dict)
    when_not: dict[str, tuple[str, ...]] = field(default_factory=dict)
    optional: bool = False
    #: Recorded in the result and injected into the plan as a constraint.
    output_key: str | None = None

    def applies(self, answers: Mapping[str, Any]) -> bool:
        for axis, accepted in self.when.items():
            value = str(answers.get(axis, ""))
            if value not in accepted:
                return False
        for axis, rejected in self.when_not.items():
            if str(answers.get(axis, "")) in rejected:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "kind": self.kind, "title": self.title}
        if self.description:
            payload["description"] = self.description
        if self.target:
            payload["target"] = self.target
        if self.options:
            payload["options"] = [{"id": key, "label": label} for key, label in self.options]
        if self.default is not None:
            payload["default"] = self.default
        if self.tool:
            payload["tool"] = self.tool
        if self.args:
            payload["args"] = self.args
        if self.gate:
            payload["gate"] = self.gate
        if self.depends_on:
            payload["depends_on"] = self.depends_on
        if self.when:
            payload["when"] = {k: list(v) for k, v in self.when.items()}
        if self.when_not:
            payload["when_not"] = {k: list(v) for k, v in self.when_not.items()}
        if self.optional:
            payload["optional"] = True
        if self.output_key:
            payload["output_key"] = self.output_key
        return payload


@dataclass(slots=True)
class WorkflowSpec:
    """A complete guided workflow."""

    id: str
    title: str
    purpose: str
    steps: list[WorkflowStep] = field(default_factory=list)
    tags: tuple[str, ...] = ()
    outputs: list[str] = field(default_factory=list)
    #: Where this spec came from: ``library`` | ``generated`` | ``project`` | ``user``.
    source: str = "library"
    #: Human-readable record of what was adapted and why (generated specs only).
    adaptations: list[str] = field(default_factory=list)
    estimated_minutes: int = 0
    created_at: float = field(default_factory=time.time)

    def step(self, step_id: str) -> WorkflowStep | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def applicable_steps(self, answers: Mapping[str, Any]) -> list[WorkflowStep]:
        return [step for step in self.steps if step.applies(answers)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "purpose": self.purpose,
            "tags": list(self.tags),
            "outputs": self.outputs,
            "source": self.source,
            "adaptations": self.adaptations,
            "estimated_minutes": self.estimated_minutes,
            "steps": [step.to_dict() for step in self.steps],
        }

    def to_yaml(self) -> str:
        return spec_to_yaml(self)


@dataclass(slots=True)
class WorkflowToolCall:
    """A tool invocation a workflow asked for (executed by the caller)."""

    step_id: str
    tool: str
    args: dict[str, Any]
    executed: bool = False
    result: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "args": self.args,
            "executed": self.executed,
            "error": self.error,
        }


@dataclass(slots=True)
class WorkflowResult:
    """Everything a workflow run produced."""

    workflow: str
    answers: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    tool_calls: list[WorkflowToolCall] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    aborted: bool = False
    constraints: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "answers": self.answers,
            "notes": self.notes,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "gates": self.gates,
            "skipped": self.skipped,
            "constraints": self.constraints,
            "duration_s": self.duration_s,
            "aborted": self.aborted,
        }


# --------------------------------------------------------------------------- #
# the shipped library
# --------------------------------------------------------------------------- #
def _s(**kwargs: Any) -> WorkflowStep:
    return WorkflowStep(**kwargs)


LIBRARY: dict[str, WorkflowSpec] = {}


def _register(spec: WorkflowSpec) -> WorkflowSpec:
    LIBRARY[spec.id] = spec
    return spec


_register(
    WorkflowSpec(
        id="new-product",
        title="New product blueprint",
        purpose="Turn a vague product idea into a decision-complete blueprint before a single file is written.",
        tags=("greenfield", "planning", "product"),
        outputs=["blueprint", "architecture", "milestones"],
        estimated_minutes=4,
        steps=[
            _s(id="shape", kind="ask", target="app_type", title="What kind of product is this?", description="Everything downstream follows from this."),
            _s(id="why", kind="ask", target="problem", title="What problem does it solve?", description="In one sentence — this becomes the README's first line."),
            _s(id="who", kind="ask", target="audience", title="Who uses it?", depends_on=["shape"]),
            _s(id="depth", kind="ask", target="feature_depth", title="How deep is version one?", depends_on=["shape"]),
            _s(id="timeline", kind="ask", target="scope_level", title="What complexity should we assume?", depends_on=["depth"]),
            _s(id="look", kind="ask", target="design_direction", title="Visual direction", depends_on=["shape"], when_not={"app_type": ("api", "library", "data-pipeline")}),
            _s(id="backend", kind="ask", target="backend_style", title="Backend shape", depends_on=["shape"]),
            _s(id="data", kind="ask", target="data_strategy", title="Where does data live?", depends_on=["backend"], when_not={"backend_style": ("static",)}),
            _s(id="auth", kind="ask", target="auth_strategy", title="How do people sign in?", depends_on=["backend"], when_not={"backend_style": ("static", "local")}),
            _s(id="auth-detail", kind="decide", title="Auth specifics", depends_on=["auth"], when={"auth_strategy": ("email-password", "magic-link", "social-oauth", "passkeys")}, options=[
                ("session-cookie", "Server sessions in an HTTP-only cookie"),
                ("jwt", "Signed tokens held by the client"),
                ("vendor-sdk", "Let the auth vendor's SDK own the session"),
            ], default="session-cookie", output_key="auth_mechanism"),
            _s(id="quality", kind="ask", target="testing_bar", title="How much verification before 'done'?"),
            _s(id="priority", kind="ask", target="performance_priority", title="What matters most at runtime?"),
            _s(id="deploy", kind="ask", target="deployment", title="Where will it be deployed?"),
            _s(id="not-building", kind="ask", target="non_goals", title="What are we deliberately not building?", optional=True),
            _s(id="confirm", kind="confirm", title="Review the blueprint", depends_on=["shape", "depth", "backend"], description="Every decision, its source and its consequence."),
            _s(id="scaffold", kind="tool", tool="inspect_project", title="Inspect the current tree", depends_on=["confirm"], description="Establish a baseline before scaffolding."),
            _s(id="syntax-gate", kind="gate", gate="syntax", title="Baseline syntax gate", depends_on=["scaffold"]),
        ],
    )
)

_register(
    WorkflowSpec(
        id="feature-add",
        title="Feature addition",
        purpose="Add a feature to an existing codebase without breaking what already works.",
        tags=("feature", "existing-repo"),
        outputs=["plan", "patch", "tests"],
        estimated_minutes=3,
        steps=[
            _s(id="scope", kind="ask", target=None, title="How big is this feature?", options=[
                ("small", "Small — one screen or one endpoint"),
                ("medium", "Medium — a few files across layers"),
                ("large", "Large — a new subsystem"),
            ], default="medium", output_key="feature_size"),
            _s(id="surface", kind="decide", title="Which surfaces does it touch?", options=[
                ("ui", "UI only"),
                ("api", "API only"),
                ("both", "UI and API"),
                ("data", "Data model / migrations"),
            ], default="both", output_key="surface"),
            _s(id="inspect", kind="tool", tool="inspect_project", title="Map the repository", description="Find the conventions this feature must follow."),
            _s(id="locate", kind="tool", tool="search_code", title="Locate the seams", args={"pattern": "{keyword}"}, depends_on=["inspect"]),
            _s(id="compat", kind="decide", title="Backwards compatibility", options=[
                ("strict", "Strict — no breaking change to any caller"),
                ("versioned", "Versioned — add the new path, deprecate the old"),
                ("breaking", "Breaking is acceptable — nothing depends on it yet"),
            ], default="versioned", output_key="compat"),
            _s(id="tests", kind="decide", title="Test strategy", options=[
                ("after", "Write tests after the implementation"),
                ("with", "Write tests alongside each unit"),
                ("before", "Write the failing test first"),
            ], default="with", output_key="test_strategy"),
            _s(id="migrate", kind="confirm", title="Migration plan", when={"surface": ("data", "both")}, description="Confirm the schema change and its rollback path before applying."),
            _s(id="verify", kind="gate", gate="tests", title="Run the test gate", depends_on=["inspect"]),
            _s(id="ui-verify", kind="gate", gate="browser", title="Browser-verify the change", when={"surface": ("ui", "both")}, depends_on=["verify"]),
        ],
    )
)

_register(
    WorkflowSpec(
        id="bug-fix",
        title="Bug hunt & fix",
        purpose="Reproduce first, then fix, then prove it cannot come back.",
        tags=("bug", "debug", "fix"),
        outputs=["reproduction", "patch", "regression-test"],
        estimated_minutes=3,
        steps=[
            _s(id="symptom", kind="ask", target=None, title="What is the observable symptom?", options=[
                ("crash", "Crash / traceback"),
                ("wrong-result", "Wrong result, no crash"),
                ("ui", "UI looks or behaves wrong"),
                ("perf", "Too slow"),
                ("flaky", "Intermittent"),
                ("security", "Security / data exposure"),
            ], default="wrong-result", output_key="symptom"),
            _s(id="repro", kind="confirm", title="Can you reproduce it?", description="A fix without a reproduction is a guess. If not reproducible, the first task is instrumentation."),
            _s(id="repro-steps", kind="tool", tool="read_logs", title="Collect the evidence", args={"lines": 200}, optional=True),
            _s(id="locate", kind="tool", tool="search_code", title="Find the suspect code", args={"pattern": "{keyword}"}),
            _s(id="hypothesis", kind="decide", title="Most likely cause", options=[
                ("logic", "Logic error in our code"),
                ("state", "State/ordering bug"),
                ("boundary", "Boundary condition (empty, null, overflow, timezone)"),
                ("integration", "A dependency behaved unexpectedly"),
                ("data", "Bad or migrated data"),
                ("race", "Concurrency / race"),
            ], default="logic", output_key="hypothesis"),
            _s(id="regression", kind="decide", title="Regression guard", options=[
                ("unit", "Unit test at the failure point"),
                ("integration", "Integration test through the real path"),
                ("browser", "Browser flow reproducing the user's steps"),
                ("all", "All three"),
            ], default="unit", output_key="regression"),
            _s(id="blast", kind="confirm", title="Blast radius", description="Confirm what else uses the code being changed."),
            _s(id="verify", kind="gate", gate="tests", title="Prove the fix"),
            _s(id="browser-verify", kind="gate", gate="browser", when={"symptom": ("ui",)}, title="Browser-verify the symptom is gone", depends_on=["verify"]),
        ],
    )
)

_register(
    WorkflowSpec(
        id="refactor",
        title="Safe refactor",
        purpose="Change the structure without changing the behaviour — and prove it.",
        tags=("refactor", "cleanup", "architecture"),
        outputs=["baseline", "patch", "behaviour-proof"],
        estimated_minutes=4,
        steps=[
            _s(id="motive", kind="ask", target=None, title="Why refactor?", options=[
                ("duplication", "Remove duplication"),
                ("boundary", "Fix a module boundary"),
                ("naming", "Naming and clarity"),
                ("perf", "Performance"),
                ("deps", "Remove or replace a dependency"),
                ("testability", "Make it testable"),
                ("upgrade", "Prepare for an upgrade"),
            ], default="boundary", output_key="motive"),
            _s(id="baseline", kind="tool", tool="run_gates", title="Capture a green baseline", description="Never refactor on red: you cannot tell what you broke."),
            _s(id="characterize", kind="decide", title="Characterisation tests", options=[
                ("existing", "Existing coverage is sufficient"),
                ("add", "Add tests that pin current behaviour first"),
                ("golden", "Golden-output snapshots for the affected paths"),
            ], default="add", output_key="characterisation"),
            _s(id="stride", kind="decide", title="Stride", options=[
                ("tiny", "Tiny commits, each independently green"),
                ("module", "One module at a time"),
                ("big-bang", "Single large change (fastest, riskiest)"),
            ], default="tiny", output_key="stride"),
            _s(id="scope-limit", kind="confirm", title="Explicitly out of scope", description="Name what this refactor will NOT touch, so it cannot grow."),
            _s(id="apply", kind="tool", tool="apply_patch", title="Apply the refactor", args={"patch": "{patch}"}, depends_on=["baseline"]),
            _s(id="prove", kind="gate", gate="tests", title="Prove behaviour is unchanged", depends_on=["apply"]),
            _s(id="lint", kind="gate", gate="lint", title="Lint and typecheck", depends_on=["prove"]),
            _s(id="diff-review", kind="gate", gate="diff", title="Review the diff for accidental changes", depends_on=["prove"]),
        ],
    )
)

_register(
    WorkflowSpec(
        id="ui-polish",
        title="UI/UX finish",
        purpose="Take a working interface to one that feels designed: spacing, states, hierarchy, accessibility.",
        tags=("ui", "ux", "design", "polish"),
        outputs=["tokens", "states", "evidence"],
        estimated_minutes=4,
        steps=[
            _s(id="direction", kind="ask", target="design_direction", title="Visual direction"),
            _s(id="reference", kind="ask", target="design_reference", title="Any reference to anchor to?", optional=True),
            _s(id="audit", kind="tool", tool="browser_screenshot", title="Screenshot the current state", args={"url": "{url}", "full_page": True}),
            _s(id="pass", kind="decide", title="Which passes should run?", options=[
                ("tokens", "Design tokens: colour, spacing, type scale"),
                ("states", "Loading / empty / error / disabled states"),
                ("hierarchy", "Visual hierarchy and alignment"),
                ("responsive", "Responsive breakpoints"),
                ("a11y", "Contrast, focus order, landmarks"),
                ("motion", "Transitions and reduced-motion support"),
                ("all", "All of the above"),
            ], default="all", output_key="passes"),
            _s(id="contrast", kind="gate", gate="browser", title="Contrast and console-error budget", depends_on=["audit"], when={"direction": ("accessible-first", "glass-neumorphic", "premium-dark")}),
            _s(id="viewport", kind="decide", title="Viewports to verify", options=[
                ("desktop", "Desktop only (1440×900)"),
                ("desktop-mobile", "Desktop + mobile (360×740)"),
                ("three", "Desktop + tablet + mobile"),
            ], default="desktop-mobile", output_key="viewports"),
            _s(id="after", kind="tool", tool="browser_screenshot", title="Screenshot after the pass", args={"url": "{url}", "full_page": True}),
            _s(id="compare", kind="confirm", title="Before / after comparison", description="Approve the visual change before it is committed."),
        ],
    )
)

_register(
    WorkflowSpec(
        id="integration",
        title="Third-party integration",
        purpose="Wire an external system with the failure modes handled before they happen.",
        tags=("integration", "api", "payments", "webhooks"),
        outputs=["client", "fakes", "runbook"],
        estimated_minutes=5,
        steps=[
            _s(id="which", kind="ask", target="integrations", title="What are we integrating?"),
            _s(id="mode", kind="decide", title="Interaction mode", options=[
                ("request", "We call them (request/response)"),
                ("webhook-in", "They call us (inbound webhooks)"),
                ("webhook-out", "We notify them (outbound webhooks)"),
                ("both", "Both directions"),
                ("stream", "Long-lived stream / socket"),
            ], default="request", output_key="mode"),
            _s(id="secrets", kind="decide", title="Credential handling", options=[
                ("env", "Environment variables"),
                ("store", "Unknown Agent credential store (0600)"),
                ("keyring", "OS keyring"),
                ("vault", "External secret manager"),
            ], default="store", output_key="secrets"),
            _s(id="sandbox", kind="confirm", title="Sandbox credentials available?", description="Without a sandbox, verification means calling production. Say so explicitly."),
            _s(id="signature", kind="decide", title="Webhook verification", when={"mode": ("webhook-in", "both")}, options=[
                ("hmac", "HMAC signature with timestamp + replay window"),
                ("vendor-sdk", "Vendor SDK verifier"),
                ("mtls", "Mutual TLS"),
                ("none", "None (documented risk)"),
            ], default="hmac", output_key="webhook_verification"),
            _s(id="idempotency", kind="decide", title="Idempotency", when={"mode": ("webhook-in", "both", "request")}, options=[
                ("key", "Idempotency key on every mutating call"),
                ("dedupe", "Deduplicate inbound events by id"),
                ("both", "Both"),
                ("none", "Not applicable"),
            ], default="both", output_key="idempotency"),
            _s(id="failure", kind="decide", title="Failure policy", options=[
                ("retry", "Retry with backoff, then dead-letter"),
                ("circuit", "Circuit breaker + cached fallback"),
                ("fail-fast", "Fail fast and surface the error"),
                ("degrade", "Degrade gracefully without the integration"),
            ], default="retry", output_key="failure_policy"),
            _s(id="fakes", kind="tool", tool="write_file", title="Write the test double", args={"path": "{fake_path}"}, description="The suite must pass with the network disabled."),
            _s(id="verify", kind="gate", gate="tests", title="Run the integration tests offline"),
            _s(id="runbook", kind="note", title="Runbook", description="Document: what breaks when they are down, how to rotate credentials, how to replay a missed event."),
        ],
    )
)

_register(
    WorkflowSpec(
        id="release",
        title="Release & deployment",
        purpose="Ship deliberately: version, changelog, artefacts, deploy, verify in the real environment.",
        tags=("release", "deploy", "ship"),
        outputs=["version", "changelog", "artefact", "verification"],
        estimated_minutes=5,
        steps=[
            _s(id="bump", kind="decide", title="Version bump", options=[
                ("patch", "Patch — fixes only"),
                ("minor", "Minor — backwards-compatible features"),
                ("major", "Major — breaking changes"),
                ("prerelease", "Pre-release (rc/beta)"),
            ], default="minor", output_key="bump"),
            _s(id="gates", kind="gate", gate="tests", title="Full gate suite must be green"),
            _s(id="changelog", kind="tool", tool="write_file", title="Write the changelog entry", args={"path": "CHANGELOG.md"}, depends_on=["gates"]),
            _s(id="artefact", kind="tool", tool="build_run", title="Build the release artefact", depends_on=["changelog"]),
            _s(id="target", kind="ask", target="deployment", title="Where are we deploying?"),
            _s(id="dry-run", kind="confirm", title="Dry-run the deploy first", description="Show exactly what will change in the target environment."),
            _s(id="deploy", kind="tool", tool="shell_run", title="Deploy", args={"command": "{deploy_command}"}, depends_on=["dry-run"]),
            _s(id="smoke", kind="gate", gate="browser", title="Smoke-test the deployed environment", depends_on=["deploy"], optional=True),
            _s(id="rollback", kind="note", title="Rollback plan", description="Record how to revert within one minute, and who decides."),
            _s(id="tag", kind="tool", tool="git_commit", title="Commit and tag the release", args={"message": "release: {version}"}, depends_on=["artefact"]),
        ],
    )
)

_register(
    WorkflowSpec(
        id="security-audit",
        title="Security & data audit",
        purpose="Find the ways this system leaks, breaks or exposes data — before someone else does.",
        tags=("security", "audit", "compliance"),
        outputs=["threat-model", "findings", "fixes"],
        estimated_minutes=6,
        steps=[
            _s(id="surface", kind="tool", tool="inspect_project", title="Enumerate the attack surface", description="Endpoints, secrets, uploads, deserialization, shell-outs."),
            _s(id="threats", kind="decide", title="Which threat classes are in scope?", options=[
                ("authz", "Broken authorisation (IDOR, privilege escalation)"),
                ("injection", "Injection (SQL, command, template)"),
                ("secrets", "Secret exposure (logs, repo, responses)"),
                ("ssrf", "SSRF / outbound request forgery"),
                ("supply", "Dependency and supply chain"),
                ("data", "Data retention, PII and deletion"),
                ("all", "All of the above"),
            ], default="all", output_key="threats"),
            _s(id="secrets-scan", kind="tool", tool="search_code", title="Scan for hardcoded secrets", args={"pattern": "(?i)(api[_-]?key|secret|token|password)\\s*[:=]"}),
            _s(id="authz-tests", kind="tool", tool="test_run", title="Authorisation tests: every endpoint denies the wrong role", depends_on=["surface"]),
            _s(id="deps", kind="tool", tool="package_info", title="Dependency audit"),
            _s(id="logging", kind="confirm", title="Confirm nothing sensitive reaches logs or error responses"),
            _s(id="report", kind="note", title="Findings report", description="Severity, evidence, remediation and a verification step for each finding."),
        ],
    )
)

_register(
    WorkflowSpec(
        id="docs-handover",
        title="Documentation & handover",
        purpose="Make the project runnable and changeable by someone who was not in the room.",
        tags=("docs", "handover", "onboarding"),
        outputs=["readme", "runbook", "architecture-notes"],
        estimated_minutes=3,
        steps=[
            _s(id="who", kind="ask", target="maintenance", title="Who takes this over?"),
            _s(id="audit", kind="tool", tool="inspect_project", title="Audit what exists", description="Commands, entry points, env vars, deployment target."),
            _s(id="fresh-clone", kind="tool", tool="shell_run", title="Fresh-clone test", args={"command": "{clone_command}"}, description="Every command in the README must actually work."),
            _s(id="sections", kind="decide", title="Which documents are needed?", options=[
                ("readme", "README with quickstart and commands"),
                ("architecture", "Architecture notes and module boundaries"),
                ("runbook", "Operational runbook (deploy, rollback, on-call)"),
                ("adr", "Architecture decision records"),
                ("contributing", "Contributing guide"),
                ("all", "All of the above"),
            ], default="readme", output_key="sections"),
            _s(id="decisions", kind="note", title="Record the decisions that are not obvious from the code", description="Why this stack, why this shape, what was rejected."),
            _s(id="verify", kind="confirm", title="Read it as a stranger", description="Would you be able to run, test and deploy this from the docs alone?"),
        ],
    )
)


#: Task-text triggers → workflow id, with the signals that justify the match.
WORKFLOW_TRIGGERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "new-product": (
        ("build", "create", "make", "start", "new"),
        ("app", "application", "website", "site", "product", "saas", "platform", "tool", "startup", "idea",
         "ecommerce", "e-commerce", "store", "shop", "dashboard", "portal", "service", "api", "cli"),
    ),
    "feature-add": (
        ("add", "implement", "introduce", "support", "extend", "build out"),
        ("feature", "endpoint", "screen", "page", "button", "flow", "capability"),
    ),
    "bug-fix": (
        ("fix", "debug", "broken", "failing", "crash", "bug", "error", "500", "regression", "flaky"),
        ("test", "page", "endpoint", "flow", "build", "deploy", "it", "this"),
    ),
    "refactor": (
        ("refactor", "restructure", "clean up", "cleanup", "simplify", "reorganize", "extract", "decouple", "modularize"),
        ("code", "module", "structure", "architecture", "duplication", "mess"),
    ),
    "ui-polish": (
        ("polish", "improve", "redesign", "restyle", "prettify", "fix the ui", "ui feels", "make it look"),
        ("ui", "design", "layout", "spacing", "css", "styling", "look", "landing", "dashboard"),
    ),
    "integration": (
        ("integrate", "connect", "wire up", "hook up", "add support for", "plug in", "sync with", "send to",
         "subscribe to", "listen for"),
        ("stripe", "api", "webhook", "oauth", "twilio", "sendgrid", "s3", "slack", "payments", "third-party", "sdk"),
    ),
    "release": (
        ("release", "ship", "deploy", "publish", "version", "tag", "cut a release", "roll out"),
        ("v1", "1.0", "production", "staging", "package", "artefact", "artifact"),
    ),
    "security-audit": (
        ("audit", "security review", "harden", "vulnerability", "pentest", "threat model", "compliance"),
        ("security", "secrets", "auth", "permissions", "gdpr", "pii", "data"),
    ),
    "docs-handover": (
        ("document", "write docs", "handover", "hand over", "onboard", "explain the codebase"),
        ("readme", "docs", "runbook", "contributing", "adr", "team"),
    ),
}


def library_payload() -> list[dict[str, Any]]:
    return [spec.to_dict() for spec in LIBRARY.values()]


def get_workflow(workflow_id: str) -> WorkflowSpec:
    key = (workflow_id or "").strip().lower()
    if key in LIBRARY:
        return LIBRARY[key]
    for spec in LIBRARY.values():
        if key in spec.tags or key == spec.title.lower():
            return spec
    raise KeyError(workflow_id)


# --------------------------------------------------------------------------- #
# detection and generation
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class WorkflowMatch:
    """Why a workflow was chosen."""

    workflow_id: str
    score: float
    triggers: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"workflow_id": self.workflow_id, "score": round(self.score, 2), "triggers": self.triggers, "reason": self.reason}


def detect(task: str, *, answers: Mapping[str, Any] | None = None) -> WorkflowMatch | None:
    """Find the workflow shape a task has, or ``None`` if it has none.

    A match needs a verb *and* an object from the trigger table — "fix the bug"
    matches bug-fix, "fix" alone does not.  That keeps detection from firing on
    every sentence that happens to contain a common word.
    """

    lowered = (task or "").lower()
    known = dict(answers or {})
    best: WorkflowMatch | None = None
    for workflow_id, groups in WORKFLOW_TRIGGERS.items():
        hits: list[str] = []
        score = 0.0
        matched_groups = 0
        # The first group is the *verb* group: it carries the intent, so it is
        # weighted heaviest and a workflow never matches without it.  That is what
        # stops "hand over the team's new ecommerce site" from becoming a docs task.
        for position, group in enumerate(groups):
            group_hits = [needle for needle in group if needle in lowered]
            if not group_hits:
                continue
            matched_groups += 1
            weight = 2.0 if position == 0 else 1.0
            score += weight + 0.25 * (len(group_hits) - 1)
            hits.extend(group_hits[:2])
        primary_matched = any(needle in lowered for needle in groups[0])
        distinct_hits = len({hit for hit in hits if not hit.startswith("tag:")})
        # Intent is normally carried by the verb group, but two distinct strong nouns
        # ("stripe" + "webhook") name the work just as surely.  A single incidental
        # word never matches, which is what keeps "…and a handover to a team" from
        # turning a build request into a documentation request.
        if not primary_matched and distinct_hits < 2:
            continue
        if matched_groups < 1:
            continue
        spec = LIBRARY[workflow_id]
        for tag in spec.tags:
            if tag in lowered:
                score += 0.5
                hits.append(f"tag:{tag}")
        # A greenfield build signal outranks incidental mentions of other shapes —
        # unless the request is about how something *looks*, which means the thing
        # already exists and the work is design, not construction.
        design_intent = any(
            marker in lowered
            for marker in ("look", "looks", "feel", "feels", "visual", "design", "styling", "ui ", "prettier", "nicer", "layout")
        )
        if workflow_id == "new-product":
            if design_intent:
                score -= 1.0
            elif primary_matched and matched_groups >= 2:
                score += 1.0
            if known.get("app_type") and not design_intent:
                score += 0.75
        elif workflow_id == "ui-polish" and design_intent:
            score += 1.0
        if best is None or score > best.score:
            best = WorkflowMatch(
                workflow_id=workflow_id,
                score=score,
                triggers=hits[:6],
                reason=f"matched {', '.join(repr(hit) for hit in hits[:3])} in the request",
            )
    if best is None and known.get("app_type"):
        best = WorkflowMatch(workflow_id="new-product", score=0.5, reason="a blueprint exists but the task text has no workflow shape")
    return best


def _clone(spec: WorkflowSpec) -> WorkflowSpec:
    return WorkflowSpec(
        id=spec.id,
        title=spec.title,
        purpose=spec.purpose,
        steps=[
            WorkflowStep(
                id=step.id,
                kind=step.kind,
                title=step.title,
                description=step.description,
                target=step.target,
                options=list(step.options),
                default=step.default,
                tool=step.tool,
                args=dict(step.args),
                gate=step.gate,
                depends_on=list(step.depends_on),
                when={k: tuple(v) for k, v in step.when.items()},
                when_not={k: tuple(v) for k, v in step.when_not.items()},
                optional=step.optional,
                output_key=step.output_key,
            )
            for step in spec.steps
        ],
        tags=spec.tags,
        outputs=list(spec.outputs),
        source=spec.source,
        adaptations=list(spec.adaptations),
        estimated_minutes=spec.estimated_minutes,
    )


def generate(
    task: str,
    *,
    answers: Mapping[str, Any] | None = None,
    workflow_id: str | None = None,
    keyword: str = "",
    url: str = "",
    extra_steps: Iterable[WorkflowStep] = (),
) -> WorkflowSpec:
    """Synthesise a workflow for *this* task and *these* decisions.

    Three things happen:

    1. **Select** — an explicit id, else :func:`detect`, else ``new-product``.
    2. **Parameterise** — placeholders in tool arguments are filled from the
       answers and the task, so the generated spec is immediately runnable.
    3. **Adapt** — append the steps the answers imply but the library shape does
       not include (payments → webhook verification; UI → browser gate;
       handover → docs; strict a11y → contrast gate).  Every adaptation is
       recorded in ``adaptations`` so the user can see what was added and why.
    """

    known: dict[str, Any] = {key: str(value) for key, value in dict(answers or {}).items()}
    chosen_id = (workflow_id or "").strip().lower()
    if not chosen_id:
        match = detect(task, answers=known)
        chosen_id = match.workflow_id if match else "new-product"
    if chosen_id not in LIBRARY:
        chosen_id = "new-product"
    spec = _clone(LIBRARY[chosen_id])
    spec.source = "generated"
    spec.adaptations = []

    keyword = keyword or _keyword_from(task)
    replacements = {
        "{keyword}": keyword,
        "{url}": url or known.get("dev_url") or "http://localhost:3000",
        "{version}": "0.1.0",
        "{fake_path}": "tests/fakes/test_double.py",
        "{patch}": "",
        "{deploy_command}": known.get("deploy_command", "echo 'deploy not configured'"),
        "{clone_command}": "git clone <repo> && cd <repo> && <install> && <test>",
    }

    def substitute(value: Any) -> Any:
        if isinstance(value, str):
            for needle, replacement in replacements.items():
                value = value.replace(needle, replacement)
            return value
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value

    for step in spec.steps:
        step.args = substitute(step.args)
        if step.description:
            step.description = substitute(step.description)
        if keyword and "{keyword}" in step.title:
            step.title = substitute(step.title)

    # -- adaptations driven by the blueprint --------------------------------
    app_type = known.get("app_type", "")
    has_ui = app_type in {"web-app", "static-site", "desktop-app", "mobile-app", "game"} or chosen_id in {"ui-polish", "feature-add"}
    integrations = known.get("integrations", "")
    auth = known.get("auth_strategy", "")
    monetization = known.get("monetization", "")
    a11y = known.get("accessibility", "")
    maintenance = known.get("maintenance", "")
    testing = known.get("testing_bar", "")
    backend = known.get("backend_style", "")

    existing_ids = {step.id for step in spec.steps}

    def append(step: WorkflowStep, reason: str) -> None:
        if step.id in existing_ids:
            return
        spec.steps.append(step)
        existing_ids.add(step.id)
        spec.adaptations.append(f"+ {step.title} — {reason}")

    if has_ui and not any(step.gate == "browser" for step in spec.steps):
        append(
            _s(id="adapt-browser", kind="gate", gate="browser", title="Browser validation pass", description="Core journey in real Chromium: zero console errors, zero failed requests, screenshot evidence."),
            f"{app_type or 'this product'} has a UI that must be proven in a real browser",
        )
    if monetization in {"subscription", "usage", "commerce", "freemium", "one-time"} and chosen_id != "integration":
        append(
            _s(id="adapt-payments", kind="decide", title="Billing mechanics", options=[
                ("checkout-session", "Hosted checkout session"),
                ("embedded", "Embedded payment form"),
                ("invoice", "Invoicing only"),
            ], default="checkout-session", output_key="billing_mechanics"),
            f"monetisation is '{monetization}', so billing needs its own decisions",
        )
        append(
            _s(id="adapt-webhooks", kind="decide", title="Payment webhook verification", options=[
                ("hmac", "Verify the vendor signature with a timestamp window"),
                ("sdk", "Use the vendor SDK verifier"),
            ], default="sdk", output_key="payment_webhook_verification"),
            "money moving without verified webhooks is how double-charges happen",
        )
        append(
            _s(id="adapt-idempotency", kind="confirm", title="Idempotency of every money-moving path", description="Confirm each mutating payment call carries an idempotency key and each event is deduplicated."),
            "retries and duplicate webhooks must not charge twice",
        )
    if auth not in {"", "none"} and chosen_id != "security-audit":
        append(
            _s(id="adapt-authz", kind="tool", tool="test_run", title="Authorisation denial tests", args={"selector": "auth"}, description="Every protected route must refuse the wrong identity, proven by a test."),
            f"authentication is '{auth}': denial paths need executable proof",
        )
        append(
            _s(id="adapt-secrets", kind="confirm", title="No secret reaches a log or an error response", description="Confirm credential handling and redaction before this ships."),
            "credentials are in scope",
        )
    if a11y in {"aa", "strict"} and has_ui:
        append(
            _s(id="adapt-a11y", kind="gate", gate="browser", title="Accessibility gate", description="Contrast ratios measured, keyboard-only completion of the core flow, landmarks present."),
            f"accessibility bar is '{a11y}'",
        )
    if maintenance in {"handover", "team", "oss"} and chosen_id not in {"docs-handover", "release"}:
        append(
            _s(id="adapt-docs", kind="tool", tool="write_file", title="Handover documentation", args={"path": "README.md"}, description="Quickstart, commands, architecture notes and the decisions behind them."),
            f"someone else maintains this ('{maintenance}')",
        )
    if testing == "tdd":
        append(
            _s(id="adapt-tdd", kind="decide", title="Red-green order", options=[
                ("strict", "Every unit gets a failing test first"),
                ("risky-first", "Only the risky paths are test-first"),
            ], default="risky-first", output_key="tdd_order"),
            "test-first was chosen as the verification bar",
        )
    if backend == "microservices":
        append(
            _s(id="adapt-contracts", kind="tool", tool="test_run", title="Contract tests between services", args={"selector": "contract"}, description="No service may deploy while a consumer's contract test is red."),
            "microservices without contract tests drift apart silently",
        )
    if known.get("performance_priority") == "seo" and has_ui:
        append(
            _s(id="adapt-seo", kind="gate", gate="browser", title="SEO & Core Web Vitals check", description="Real metadata per route, server-rendered content, Lighthouse budgets."),
            "SEO was chosen as the runtime priority",
        )

    for step in extra_steps:
        append(step, "requested by the caller")

    spec.estimated_minutes = max(1, len(spec.applicable_steps(known)) // 2)
    spec.outputs = sorted(set(spec.outputs) | {step.output_key for step in spec.steps if step.output_key})
    return spec


def _keyword_from(task: str) -> str:
    """A search keyword from the task, for ``search_code`` steps."""

    words = [word.strip(".,!?()[]{}\"'`") for word in (task or "").split()]
    stop = {
        "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "with", "please", "make", "build",
        "create", "add", "fix", "implement", "write", "our", "my", "your", "it", "this", "that", "is", "are",
        "be", "should", "would", "can", "we", "i", "you", "app", "application", "website", "site", "feature",
    }
    candidates = [word for word in words if word.lower() not in stop and len(word) > 3]
    return candidates[0] if candidates else (words[0] if words else "TODO")


# --------------------------------------------------------------------------- #
# validation and serialisation
# --------------------------------------------------------------------------- #
def validate_spec(spec: WorkflowSpec) -> list[str]:
    """Structural problems a hand-written or generated spec might have."""

    problems: list[str] = []
    seen: set[str] = set()
    kinds = {"ask", "confirm", "tool", "gate", "note", "generate", "decide"}
    for step in spec.steps:
        if not step.id:
            problems.append("a step has no id")
            continue
        if step.id in seen:
            problems.append(f"duplicate step id {step.id!r}")
        seen.add(step.id)
        if step.kind not in kinds:
            problems.append(f"step {step.id!r} has unknown kind {step.kind!r}")
        if step.kind == "tool" and not step.tool:
            problems.append(f"step {step.id!r} is a tool step with no tool")
        if step.kind == "gate" and not step.gate:
            problems.append(f"step {step.id!r} is a gate step with no gate")
        if step.kind in {"ask", "decide"} and not step.target and not step.options:
            problems.append(f"step {step.id!r} asks a question with neither an axis target nor options")
        for dependency in step.depends_on:
            if dependency not in {other.id for other in spec.steps}:
                problems.append(f"step {step.id!r} depends on unknown step {dependency!r}")
    # cycle detection over depends_on
    graph = {step.id: list(step.depends_on) for step in spec.steps}
    state: dict[str, int] = {}

    def visit(node: str, stack: tuple[str, ...]) -> bool:
        if state.get(node) == 2:
            return False
        if state.get(node) == 1 or node in stack:
            return True
        state[node] = 1
        for parent in graph.get(node, []):
            if visit(parent, stack + (node,)):
                return True
        state[node] = 2
        return False

    for step_id in graph:
        if visit(step_id, ()):
            problems.append(f"dependency cycle involving {step_id!r}")
            break
    return problems


def spec_to_yaml(spec: WorkflowSpec) -> str:
    """Serialise a spec as YAML (via PyYAML when present, else a minimal emitter)."""

    payload = spec.to_dict()
    try:
        import yaml

        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
    except ImportError:  # pragma: no cover - PyYAML is a hard dependency in pyproject
        return _mini_yaml(payload)


def _mini_yaml(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, Mapping):
        lines = []
        for key, item in value.items():
            if isinstance(item, (Mapping, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_mini_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(line for line in lines if line)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (Mapping, list)):
                rendered = _mini_yaml(item, indent + 1).lstrip()
                lines.append(f"{pad}- {rendered}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_scalar(value)}"


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in ":#\n") or text.strip() != text:
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def spec_from_dict(payload: Mapping[str, Any], *, source: str = "project") -> WorkflowSpec:
    """Parse a spec from YAML/JSON data (project and user workflow libraries)."""

    steps: list[WorkflowStep] = []
    for raw in payload.get("steps") or []:
        if not isinstance(raw, Mapping):
            continue
        options = []
        for option in raw.get("options") or []:
            if isinstance(option, Mapping):
                options.append((str(option.get("id", "")), str(option.get("label", ""))))
            elif isinstance(option, (list, tuple)) and len(option) == 2:
                options.append((str(option[0]), str(option[1])))
        steps.append(
            WorkflowStep(
                id=str(raw.get("id", "")),
                kind=str(raw.get("kind", "note")),
                title=str(raw.get("title", "")),
                description=str(raw.get("description", "")),
                target=raw.get("target"),
                options=options,
                default=raw.get("default"),
                tool=raw.get("tool"),
                args=dict(raw.get("args") or {}),
                gate=raw.get("gate"),
                depends_on=[str(item) for item in (raw.get("depends_on") or [])],
                when={str(k): tuple(str(v) for v in vals) for k, vals in (raw.get("when") or {}).items()},
                when_not={str(k): tuple(str(v) for v in vals) for k, vals in (raw.get("when_not") or {}).items()},
                optional=bool(raw.get("optional", False)),
                output_key=raw.get("output_key"),
            )
        )
    return WorkflowSpec(
        id=str(payload.get("id", "custom")),
        title=str(payload.get("title", "Custom workflow")),
        purpose=str(payload.get("purpose", "")),
        steps=steps,
        tags=tuple(str(tag) for tag in (payload.get("tags") or ())),
        outputs=[str(item) for item in (payload.get("outputs") or [])],
        source=source,
        adaptations=[str(item) for item in (payload.get("adaptations") or [])],
        estimated_minutes=int(payload.get("estimated_minutes", 0) or 0),
    )


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def run_workflow(
    spec: WorkflowSpec,
    *,
    ui: Any,
    answers: Mapping[str, Any] | None = None,
    executor: Any = None,
    gate_runner: Any = None,
    interactive: bool = True,
) -> WorkflowResult:
    """Execute a workflow spec.

    ``ask``/``decide`` steps go through the same :class:`AskUI` protocol as
    planning, so they render as premium panels in the TUI and as numbered prompts
    headlessly.  ``tool`` steps are handed to ``executor`` (anything with a
    ``call(name, args)`` coroutine-or-function) and ``gate`` steps to
    ``gate_runner``; when neither is supplied the calls are *recorded* rather than
    executed, which is what makes a workflow safe to preview with ``--dry-run``.
    """

    import asyncio
    import inspect as _inspect

    from unknown_agent.planning.axes import AXES
    from unknown_agent.planning.clarify import PlannedQuestion, recommend
    from unknown_agent.planning.models import Answer

    known: dict[str, Any] = {key: str(value) for key, value in dict(answers or {}).items()}
    result = WorkflowResult(workflow=spec.id)
    steps = spec.applicable_steps(known)
    applicable_ids = {step.id for step in steps}
    result.skipped = [step.id for step in spec.steps if step.id not in applicable_ids]

    for index, step in enumerate(steps, start=1):
        if step.kind in {"ask", "decide"}:
            if not interactive:
                value = step.default or (step.options[0][0] if step.options else "")
                if step.target and step.target in AXES:
                    value = recommend(step.target).option_id
                result.answers[step.output_key or step.target or step.id] = value
                known[step.output_key or step.target or step.id] = value
                continue
            if step.target and step.target in AXES:
                axis = AXES[step.target]
                rec = recommend(axis)
                planned = PlannedQuestion(axis=axis, recommendation=rec, options=list(axis.options), priority=axis.impact)
                ask_result = ui.ask(planned, index=index, total=len(steps))
                value = ask_result.answer.value
                result.notes.append(f"{step.title}: {ask_result.answer.display}")
            else:
                value = _decide_step(ui, step, index=index, total=len(steps))
            key = step.output_key or step.target or step.id
            result.answers[key] = value
            known[key] = value
        elif step.kind == "confirm":
            approved = True if not interactive else bool(ui.confirm(step.title, step.description))
            result.notes.append(f"{'confirmed' if approved else 'rejected'}: {step.title}")
            if not approved:
                result.aborted = True
                break
        elif step.kind == "tool":
            call = WorkflowToolCall(step_id=step.id, tool=str(step.tool), args=dict(step.args))
            if executor is not None:
                try:
                    outcome = executor.call(call.tool, call.args)
                    if _inspect.isawaitable(outcome):
                        outcome = asyncio.get_event_loop().run_until_complete(outcome) if not _in_loop() else None
                    call.executed = True
                    call.result = outcome
                except Exception as exc:  # pragma: no cover - depends on the executor
                    call.error = f"{type(exc).__name__}: {exc}"
            result.tool_calls.append(call)
            result.notes.append(f"tool {call.tool} {'executed' if call.executed else 'recorded'}: {step.title}")
        elif step.kind == "gate":
            outcome = None
            if gate_runner is not None:
                try:
                    outcome = gate_runner.run(str(step.gate))
                except Exception as exc:  # pragma: no cover
                    outcome = f"error: {exc}"
            result.gates.append(f"{step.gate}:{'recorded' if outcome is None else outcome}")
            result.notes.append(f"gate {step.gate}: {step.title}")
        elif step.kind == "note":
            result.notes.append(f"note: {step.title} — {step.description}".rstrip(" —"))
        elif step.kind == "generate":
            result.notes.append(f"generate: {step.title}")

    result.constraints = _constraints_from(result, spec)
    result.finished_at = time.time()
    return result


def _in_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _decide_step(ui: Any, step: WorkflowStep, *, index: int, total: int) -> str:
    """Ask an inline-options step through the same protocol as an axis question."""

    if hasattr(ui, "decide"):
        return str(ui.decide(step.title, step.description, step.options, default=step.default, index=index, total=total))
    # AskUI-shaped fallback: reuse notify + a plain prompt so any UI works.
    labels = "\n".join(f"  {position}. {label}" for position, (_key, label) in enumerate(step.options, start=1))
    if hasattr(ui, "notify"):
        ui.notify(f"{step.title}\n{labels}")
    from unknown_agent.ui.prompts import Prompter

    prompter = Prompter()
    if prompter.non_interactive:
        return str(step.default or (step.options[0][0] if step.options else ""))
    raw = prompter.text(f"{step.title} [{step.default or 1}]", default=str(step.default or "1"), allow_empty=True)
    for key, _label in step.options:
        if raw.strip().lower() == key:
            return key
    try:
        position = int(raw) - 1
        if 0 <= position < len(step.options):
            return step.options[position][0]
    except ValueError:
        return raw.strip() or str(step.default or "")
    return str(step.default or "")


def _constraints_from(result: WorkflowResult, spec: WorkflowSpec) -> list[str]:
    """Turn workflow answers into constraints the planner can consume."""

    constraints: list[str] = []
    for key, value in result.answers.items():
        if value:
            constraints.append(f"{key}={value}")
    for gate in result.gates:
        constraints.append(f"gate {gate.split(':')[0]} must pass")
    return constraints
