"""Deep planning: from a blueprint to a reviewable, executable project plan.

This is the layer that makes Unknown Agent feel different from a code generator.
Given a :class:`~unknown_agent.planning.models.Blueprint` (what the user decided),
it produces a :class:`ProjectPlan`: several *candidate approaches* with scored
tradeoffs, a chosen architecture, milestones with acceptance criteria, the
verification strategy, an honest effort estimate and a risk register.

Two rules shape the design:

**Determinism first.**  :func:`synthesize` is pure Python — no network, no model.
It always produces a complete, defensible plan, which means planning works on an
air-gapped machine, in CI, in tests, and when the API key is broken.  The model is
an *enhancement*: :class:`DeepPlanner` asks it for alternatives and critique, then
validates and merges that into the deterministic skeleton.

**Tradeoffs are numbers, not adjectives.**  Every approach is scored on the same
six axes (time-to-value, risk, maintainability, fit-to-answers, operating cost,
learning curve) using weights derived from the blueprint.  "Recommended" is
therefore an auditable claim: ``ua plan --explain`` shows the matrix.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from unknown_agent.planning.axes import AXES
from unknown_agent.planning.clarify import (
    SignalSet,
    consistency_warnings,
    extract_signals,
    fill_defaults,
)
from unknown_agent.planning.models import Answer, Blueprint, DecisionOption

__all__ = [
    "Approach",
    "Milestone",
    "ArchitectureDecision",
    "ProjectPlan",
    "EffortEstimate",
    "SCORE_KEYS",
    "SCORE_LABELS",
    "synthesize",
    "score_approach",
    "DeepPlanner",
    "to_core_plan",
]


SCORE_KEYS: tuple[str, ...] = (
    "time_to_value",
    "risk",
    "maintainability",
    "fit",
    "operating_cost",
    "learning_curve",
)

SCORE_LABELS: dict[str, str] = {
    "time_to_value": "Time to first value",
    "risk": "Delivery risk",
    "maintainability": "Maintainability",
    "fit": "Fit to your answers",
    "operating_cost": "Operating cost",
    "learning_curve": "Learning curve",
}


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Approach:
    """One candidate way to build the thing."""

    id: str
    label: str
    summary: str
    stack: dict[str, str] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    tradeoffs: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    risks: list[str] = field(default_factory=list)
    effort_days: float = 0.0
    rationale: str = ""
    recommended: bool = False
    source: str = "heuristic"  # heuristic | model | user

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "summary": self.summary,
            "stack": self.stack,
            "steps": self.steps,
            "tradeoffs": {k: round(v, 2) for k, v in self.tradeoffs.items()},
            "weights": {k: round(v, 2) for k, v in self.weights.items()},
            "total": round(self.total, 2),
            "risks": self.risks,
            "effort_days": round(self.effort_days, 1),
            "rationale": self.rationale,
            "recommended": self.recommended,
            "source": self.source,
        }


@dataclass(slots=True)
class ArchitectureDecision:
    """One decision, the choice made, and why — the record a reviewer needs."""

    topic: str
    decision: str
    rationale: str
    alternatives: list[str] = field(default_factory=list)
    source: str = "blueprint"

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "decision": self.decision,
            "rationale": self.rationale,
            "alternatives": self.alternatives,
            "source": self.source,
        }


@dataclass(slots=True)
class Milestone:
    """A shippable increment with its own definition of done."""

    id: str
    title: str
    outcome: str
    deliverables: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    effort_hours: float = 0.0
    gates: list[str] = field(default_factory=list)
    parallel_group: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "outcome": self.outcome,
            "deliverables": self.deliverables,
            "acceptance": self.acceptance,
            "tasks": self.tasks,
            "tools": self.tools,
            "depends_on": self.depends_on,
            "effort_hours": round(self.effort_hours, 1),
            "gates": self.gates,
            "parallel_group": self.parallel_group,
        }


@dataclass(slots=True)
class EffortEstimate:
    """An honest estimate with its assumptions visible."""

    low_hours: float
    likely_hours: float
    high_hours: float
    steps: int
    assumptions: list[str] = field(default_factory=list)
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "low_hours": round(self.low_hours, 1),
            "likely_hours": round(self.likely_hours, 1),
            "high_hours": round(self.high_hours, 1),
            "steps": self.steps,
            "assumptions": self.assumptions,
            "confidence": self.confidence,
        }

    def human(self) -> str:
        def fmt(hours: float) -> str:
            if hours < 1:
                return f"{int(hours * 60)}m"
            if hours < 48:
                return f"{hours:.0f}h"
            return f"{hours / 24:.0f}d"

        return f"{fmt(self.low_hours)}–{fmt(self.high_hours)} (likely {fmt(self.likely_hours)})"


@dataclass(slots=True)
class ProjectPlan:
    """The complete planning artefact."""

    goal: str
    blueprint: Blueprint
    approaches: list[Approach] = field(default_factory=list)
    chosen: str = ""
    architecture: dict[str, str] = field(default_factory=dict)
    decisions: list[ArchitectureDecision] = field(default_factory=list)
    milestones: list[Milestone] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    estimate: EffortEstimate | None = None
    warnings: list[str] = field(default_factory=list)
    reasoning_mode: str = ""
    created_at: float = field(default_factory=time.time)
    source: str = "synthesized"

    @property
    def recommended(self) -> Approach | None:
        for approach in self.approaches:
            if approach.recommended:
                return approach
        return self.approaches[0] if self.approaches else None

    def approach(self, approach_id: str) -> Approach | None:
        for approach in self.approaches:
            if approach.id == approach_id:
                return approach
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source": self.source,
            "reasoning_mode": self.reasoning_mode,
            "chosen": self.chosen,
            "approaches": [approach.to_dict() for approach in self.approaches],
            "architecture": self.architecture,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "milestones": [milestone.to_dict() for milestone in self.milestones],
            "risks": self.risks,
            "verification": self.verification,
            "non_goals": self.non_goals,
            "estimate": self.estimate.to_dict() if self.estimate else None,
            "warnings": self.warnings,
            "blueprint": self.blueprint.to_dict(),
            "created_at": self.created_at,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, **kwargs)

    @property
    def step_count(self) -> int:
        return sum(len(milestone.tasks) for milestone in self.milestones)


# --------------------------------------------------------------------------- #
# architecture knowledge
# --------------------------------------------------------------------------- #
#: backend_style → the architecture we will actually build.
_ARCH_BY_BACKEND: dict[str, dict[str, str]] = {
    "static": {
        "runtime": "Static build output served from a CDN",
        "data": "Build-time content (files/CMS export); no runtime store",
        "api": "None. Any dynamic need becomes a third-party embed or a serverless island.",
        "auth": "None (a static site cannot hold a session)",
        "state": "URL + browser storage only",
        "deploy": "Static hosting: build artefacts uploaded, cache-busted by content hash",
    },
    "server-fullstack": {
        "runtime": "One server application owning routes, rendering and data access",
        "data": "Application database behind a repository/service layer",
        "api": "Internal service calls; HTTP endpoints only where a client needs them",
        "auth": "Session or token issued by the app itself",
        "state": "Server-authoritative; client state is a cache",
        "deploy": "One deployable unit (container or platform build)",
    },
    "api-only": {
        "runtime": "Stateless HTTP service; client is a separate deployable",
        "data": "Service-owned database, never exposed directly",
        "api": "Versioned contract (/v1) with schema-validated requests and responses",
        "auth": "Bearer token or API key with scopes",
        "state": "Client-side; the service is stateless between requests",
        "deploy": "API and client deployed independently",
    },
    "serverless": {
        "runtime": "Functions per route/job, event-triggered",
        "data": "Managed database or KV; connections pooled through a data API",
        "api": "Function handlers behind a gateway; contracts enforced at the edge",
        "auth": "Token verified at the edge, claims forwarded to the function",
        "state": "External (no process memory between invocations)",
        "deploy": "Infrastructure-as-code; per-function versions and aliases",
    },
    "baas": {
        "runtime": "Vendor-provided auth, database and storage; thin client logic",
        "data": "Vendor database with row-level security rules as the access layer",
        "api": "Vendor SDK calls; custom logic in edge functions only where needed",
        "auth": "Vendor auth with RLS policies enforced per table",
        "state": "Vendor-authoritative; client caches optimistically",
        "deploy": "Client to static/edge hosting; rules and functions via vendor CLI",
    },
    "local": {
        "runtime": "Application process on the user's machine",
        "data": "Embedded store (SQLite/file) inside the app's data directory",
        "api": "None external; internal module boundaries instead",
        "auth": "OS keychain for any secret; no user accounts by default",
        "state": "Local-first; sync is an optional later layer",
        "deploy": "Installer or package manager; migrations run on launch",
    },
    "microservices": {
        "runtime": "Independent services per bounded context, communicating over the network",
        "data": "One store per service; no cross-service queries",
        "api": "Published contracts per service plus an async event bus",
        "auth": "Gateway-issued tokens; services verify, never issue",
        "state": "Event-sourced or replicated where consistency matters",
        "deploy": "Per-service pipelines; contract tests gate every deploy",
    },
    "bff": {
        "runtime": "Thin aggregation service tailored to one client, plus upstream APIs",
        "data": "Little of its own; caches upstream responses",
        "api": "Client-shaped endpoints; upstream calls fan out and merge",
        "auth": "Token exchange with upstream services",
        "state": "Client-side; BFF is stateless",
        "deploy": "BFF deployed with the client's release cadence",
    },
}

#: app_type → the quality gates that must be green before "done".
_GATES_BY_TYPE: dict[str, list[str]] = {
    "web-app": ["syntax", "tests", "build", "browser", "diff"],
    "static-site": ["syntax", "build", "browser", "diff"],
    "api": ["syntax", "tests", "build", "diff"],
    "cli-tool": ["syntax", "tests", "lint", "build", "diff"],
    "desktop-app": ["syntax", "tests", "build", "diff"],
    "mobile-app": ["syntax", "tests", "build", "diff"],
    "library": ["syntax", "tests", "lint", "typecheck", "build", "diff"],
    "bot": ["syntax", "tests", "build", "diff"],
    "data-pipeline": ["syntax", "tests", "typecheck", "diff"],
    "game": ["syntax", "tests", "build", "browser", "diff"],
}

#: testing_bar → how many tests per milestone and whether tests come first.
_TESTING_POLICY: dict[str, tuple[str, float, bool]] = {
    "none": ("No automated verification beyond a syntax check", 0.0, False),
    "smoke": ("A smoke test per milestone covering the happy path", 1.0, False),
    "balanced": ("Tests for the risky paths: data writes, auth, money, external calls", 2.5, False),
    "thorough": ("Every acceptance criterion gets an executable check, plus browser flows", 4.0, False),
    "tdd": ("Tests are written first; implementation follows red → green", 4.0, True),
}

#: effort multipliers, from the two scope axes.
_EFFORT_BASE: dict[str, float] = {
    "prototype": 2.0,
    "mvp": 5.0,
    "core": 9.0,
    "full": 20.0,
    "platform": 45.0,
}
_EFFORT_SCOPE: dict[str, float] = {
    "hours": 0.35,
    "days": 0.7,
    "weeks-1-2": 1.0,
    "months-1-3": 1.8,
    "open-ended": 2.4,
}
_EFFORT_TYPE: dict[str, float] = {
    "web-app": 1.0,
    "static-site": 0.55,
    "api": 0.8,
    "cli-tool": 0.7,
    "desktop-app": 1.25,
    "mobile-app": 1.5,
    "library": 0.75,
    "bot": 0.85,
    "data-pipeline": 0.9,
    "game": 1.3,
}
_EFFORT_BACKEND: dict[str, float] = {
    "static": 0.5,
    "server-fullstack": 1.0,
    "api-only": 1.05,
    "serverless": 1.1,
    "baas": 0.8,
    "local": 0.95,
    "microservices": 1.9,
    "bff": 1.2,
}


def _option(axis_id: str, value: str) -> DecisionOption | None:
    axis = AXES.get(axis_id)
    return axis.option(value) if axis else None


def _effort(blueprint: Blueprint) -> tuple[EffortEstimate, list[str]]:
    """Estimate hours from the chosen options' own effort numbers.

    The estimate is derived from the same data the user saw ("Platform — effort 5"),
    so it cannot silently disagree with the choices that produced it.
    """

    depth = blueprint.value("feature_depth", "mvp")
    scope = blueprint.value("scope_level", "weeks-1-2")
    kind = blueprint.value("app_type", "web-app")
    backend = blueprint.value("backend_style", "server-fullstack")

    base = _EFFORT_BASE.get(depth, 6.0)
    hours = base * _EFFORT_SCOPE.get(scope, 1.0) * _EFFORT_TYPE.get(kind, 1.0) * _EFFORT_BACKEND.get(backend, 1.0)

    integrations = blueprint.value("integrations", "none")
    if integrations not in {"", "none"}:
        hours *= 1.2
    if blueprint.value("auth_strategy") not in {"", "none"}:
        hours += 3.0
    if blueprint.value("monetization") not in {"", "none", "free", "internal"}:
        hours += 8.0
    testing = blueprint.value("testing_bar", "balanced")
    hours *= {"none": 0.8, "smoke": 0.95, "balanced": 1.0, "thorough": 1.25, "tdd": 1.4}.get(testing, 1.0)
    if blueprint.value("accessibility") == "strict":
        hours *= 1.15

    steps = max(4, int(round(hours / 1.5)))
    assumptions = [
        f"scope assumes '{depth}' depth on a '{scope}' timeline",
        f"{kind} with a {backend} backend",
        "one developer plus Unknown Agent, no waiting on third-party approvals",
    ]
    if testing == "tdd":
        assumptions.append("test-first: slower to start, cheaper to change later")
    if blueprint.value("maintenance") == "throwaway":
        assumptions.append("throwaway: no docs, no refactoring pass, no CI")
    return (
        EffortEstimate(
            low_hours=round(hours * 0.6, 1),
            likely_hours=round(hours, 1),
            high_hours=round(hours * 1.7, 1),
            steps=steps,
            assumptions=assumptions,
            confidence="medium" if hours < 40 else "low",
        ),
        assumptions,
    )


def _stack_for(blueprint: Blueprint) -> dict[str, str]:
    """A concrete stack, honouring every stated preference."""

    kind = blueprint.value("app_type", "web-app")
    backend = blueprint.value("backend_style", "server-fullstack")
    data = blueprint.value("data_strategy", "sql")
    deploy = blueprint.value("deployment", "managed-paas")
    tech_pref = blueprint.value("tech_preference", "agent-choice")
    custom_tech = [t for t in blueprint.tech_preferences if t]

    language = "TypeScript"
    framework = "Next.js (App Router)"
    if tech_pref == "python" or any(t in {"python", "django", "flask", "fastapi"} for t in custom_tech):
        language, framework = "Python", "FastAPI"
        if kind == "web-app":
            framework = "FastAPI + Jinja2 templates (or Django if admin is wanted)"
    elif tech_pref == "go" or "go" in custom_tech:
        language, framework = "Go", "chi + sqlc"
    elif tech_pref == "rust" or "rust" in custom_tech:
        language, framework = "Rust", "Axum"
    elif tech_pref == "vue" or "vue" in custom_tech:
        language, framework = "TypeScript", "Nuxt 3"
    elif tech_pref == "svelte" or "svelte" in custom_tech:
        language, framework = "TypeScript", "SvelteKit"
    elif tech_pref == "react" or "react" in custom_tech:
        language, framework = "TypeScript", "React + Vite"

    if kind == "static-site":
        framework = "Astro" if language == "TypeScript" else "Nikola / MkDocs Material"
    if kind == "cli-tool":
        framework = "Typer + Rich" if language == "Python" else ("oclif / Commander" if language == "TypeScript" else "Cobra")
    if kind == "library":
        framework = "src-layout package" if language == "Python" else "tsup + vitest package"
    if kind == "mobile-app":
        language, framework = "TypeScript", "Expo (React Native)"
    if kind == "desktop-app":
        language, framework = "TypeScript", "Tauri (Rust shell) or Electron"
    if backend == "baas":
        framework = "Supabase (Postgres + Auth + Storage)" if language == "TypeScript" else "Supabase + FastAPI"

    stores = {
        "none": "None",
        "local-first": "SQLite (or IndexedDB in the browser)",
        "sql": "Postgres",
        "nosql": "MongoDB / DynamoDB",
        "kv-cache": "Redis",
        "search-index": "Postgres + pgvector (or Meilisearch)",
        "files": "S3-compatible object storage",
        "hybrid": "Postgres + S3-compatible object storage",
    }
    tests = {
        "Python": "pytest + pytest-asyncio",
        "TypeScript": "Vitest + Playwright",
        "Go": "go test + testify",
        "Rust": "cargo test",
    }
    return {
        "language": language,
        "framework": framework,
        "data": stores.get(data, "Postgres"),
        "tests": tests.get(language, "pytest"),
        "styling": "Tailwind CSS + design tokens" if kind in {"web-app", "static-site", "mobile-app"} else "n/a",
        "deploy": {
            "managed-paas": "Vercel / Fly.io / Railway",
            "container": "Docker image → container platform",
            "vps": "systemd service behind Caddy",
            "serverless-cloud": "AWS Lambda / Cloud Run",
            "self-host": "Installer + documented upgrade path",
            "static-cdn": "Static host / CDN",
            "local-only": "Runs locally; distributed as a package",
            "undecided": "Lowest-friction target chosen at build time",
        }.get(deploy, "Managed platform"),
    }


def _verification_for(blueprint: Blueprint) -> list[str]:
    kind = blueprint.value("app_type", "web-app")
    gates = list(_GATES_BY_TYPE.get(kind, ["syntax", "tests", "diff"]))
    testing = blueprint.value("testing_bar", "balanced")
    policy, _per, tdd = _TESTING_POLICY.get(testing, _TESTING_POLICY["balanced"])
    if testing == "none":
        gates = [gate for gate in gates if gate in {"syntax", "diff"}]

    checks = [f"Gates that must be green: {', '.join(gates) or 'none'}", policy]
    if tdd:
        checks.append("Workflow is red → green: a failing test exists before each implementation step.")
    priority = blueprint.value("performance_priority", "balanced")
    extra = {
        "seo": "Lighthouse SEO + a11y ≥ 95, real metadata on every route, no client-only content.",
        "ux": "Interaction latency < 100ms, skeletons for anything over 200ms, no layout shift.",
        "throughput": "A load test at the stated target with p95 latency recorded as evidence.",
        "cost": "Cold-start and idle cost measured; no always-on component without a reason.",
        "offline": "Airplane-mode pass: the app is usable with the network disabled.",
        "security": "Threat model written; authz checked on every endpoint by a test, not by review.",
        "a11y": "Keyboard-only completion of the core flow plus an automated a11y audit.",
        "scalability": "Horizontal-scale rehearsal: two instances, no shared in-process state.",
        "maintainability": "Module boundaries documented; a new contributor task completed from the README alone.",
        "dx": "Fresh-clone-to-running in ≤ 3 commands, verified by executing them.",
        "conversion": "Funnel instrumented end to end; each step has an event and a test.",
    }.get(priority)
    if extra:
        checks.append(extra)
    if "browser" in gates:
        checks.append(
            "Browser validation: real Chromium run against the dev server — core journey completed, "
            "zero console errors, zero failed requests, screenshot evidence attached."
        )
    if blueprint.value("accessibility") in {"aa", "strict"}:
        checks.append("Accessibility: contrast ratios measured, focus order verified, landmarks present.")
    if _handles_money(blueprint):
        # Money is the one feature where "it worked in my test" is not evidence:
        # the failure modes are asynchronous, retried and financial.
        checks.append(
            "Payments: webhook signature verified, delivery idempotent under retry, refund/cancellation "
            "path exercised, and a reconciliation check between the provider's ledger and ours."
        )
    return checks


def _handles_money(blueprint: Blueprint) -> bool:
    """Whether the build touches real money."""

    if blueprint.value("monetization") in {"subscription", "usage", "one-time", "freemium", "commerce"}:
        return True
    integrations = blueprint.answers.get("integrations")
    if integrations is not None and "payments" in str(integrations.value):
        return True
    haystack = f"{blueprint.goal} {blueprint.summary}".lower()
    return any(word in haystack for word in ("stripe", "checkout", "billing", "payment", "subscription"))


def _risks_for(blueprint: Blueprint) -> list[str]:
    risks: list[str] = []
    value = blueprint.value
    if value("auth_strategy") in {"email-password", "passkeys"}:
        risks.append("Self-hosted credential handling: hashing, reset flow, rate limiting and breach response are all in scope.")
    if value("integrations") in {"payments", "third-party", "webhooks"} or "payments" in blueprint.answers.get("integrations", Answer(axis="", value="")).detail.lower():
        risks.append("Third-party integration: webhook signature verification, idempotency and sandbox credentials must exist before it can be called done.")
    if value("backend_style") == "microservices":
        risks.append("Microservices inside a small scope usually deliver less, later; consider api-only until there is a second team.")
    if value("feature_depth") == "platform" and value("scope_level") in {"hours", "days", "weeks-1-2"}:
        risks.append("Platform depth on a short timeline: expect a partial delivery. Cut scope before cutting verification.")
    if value("app_type") == "mobile-app":
        risks.append("Store review and signing are outside the agent's control and can add days.")
    if value("design_direction") == "glass-neumorphic":
        risks.append("Translucent surfaces frequently fail contrast checks; budget for an accessibility pass.")
    if value("data_strategy") == "nosql":
        risks.append("Document stores make integrity the application's job; migration and query patterns need care.")
    if value("testing_bar") == "none":
        risks.append("No verification means 'done' is a claim, not a result. Reported as unverified.")
    if value("performance_priority") == "speed-to-launch":
        risks.append("Deliberate technical debt is being taken; it is recorded in the blueprint so it can be paid back.")
    if value("maintenance") in {"handover", "team", "oss"} and value("testing_bar") in {"none", "smoke"}:
        risks.append("Someone else maintains this with few tests: documentation and structure carry the whole load.")
    risks.extend(consistency_warnings(blueprint))
    return risks[:8]


def _milestones_for(blueprint: Blueprint, stack: Mapping[str, str], estimate: EffortEstimate) -> list[Milestone]:
    """Milestones derived from the choices, not from a fixed template."""

    kind = blueprint.value("app_type", "web-app")
    backend = blueprint.value("backend_style", "server-fullstack")
    auth = blueprint.value("auth_strategy", "none")
    data = blueprint.value("data_strategy", "sql")
    testing = blueprint.value("testing_bar", "balanced")
    design = blueprint.value("design_direction", "minimal-editorial")
    deploy = blueprint.value("deployment", "managed-paas")
    maintenance = blueprint.value("maintenance", "solo")
    depth = blueprint.value("feature_depth", "mvp")
    priority = blueprint.value("performance_priority", "balanced")
    per_test = _TESTING_POLICY.get(testing, (0, 2.5, False))[1]
    tdd = _TESTING_POLICY.get(testing, (0, 2.5, False))[2]
    total_hours = estimate.likely_hours or 8.0

    has_ui = kind in {"web-app", "static-site", "desktop-app", "mobile-app", "game"}
    has_server = backend not in {"static", "local"}
    gates = _GATES_BY_TYPE.get(kind, ["syntax", "tests", "diff"])

    milestones: list[Milestone] = []

    def add(
        mid: str,
        title: str,
        outcome: str,
        *,
        deliverables: Sequence[str] = (),
        acceptance: Sequence[str] = (),
        tasks: Sequence[str] = (),
        tools: Sequence[str] = ("read_file", "write_file", "shell_run"),
        depends_on: Sequence[str] = (),
        share: float = 0.1,
        mgates: Sequence[str] = ("syntax",),
        group: str = "",
    ) -> None:
        milestones.append(
            Milestone(
                id=mid,
                title=title,
                outcome=outcome,
                deliverables=list(deliverables),
                acceptance=list(acceptance),
                tasks=list(tasks),
                tools=list(tools),
                depends_on=list(depends_on),
                effort_hours=round(total_hours * share, 1),
                gates=list(mgates),
                parallel_group=group,
            )
        )

    add(
        "m0",
        "Foundation & contracts",
        "A runnable skeleton with the decisions from the blueprint encoded, so later steps cannot drift.",
        deliverables=[
            "Project scaffold with the chosen stack pinned",
            "BLUEPRINT.md recording every decision and its rationale",
            "Configuration and environment loading",
            "CI command list (dev/test/build/lint) wired to the repo",
        ],
        acceptance=[
            "The project builds and starts from a clean checkout with documented commands",
            "BLUEPRINT.md exists and matches .unknown/blueprint.toml",
            "A health/index route renders or responds",
        ],
        tasks=[
            "Scaffold the project with the chosen language and framework",
            "Pin dependencies and add the quality-gate commands",
            "Write BLUEPRINT.md from the persisted blueprint",
            "Add environment/config loading with sane local defaults",
        ] + (["Write the first failing test for the core journey"] if tdd else []),
        tools=["inspect_project", "write_file", "shell_run", "build_run", "test_run"],
        share=0.12,
        mgates=("syntax", "build"),
    )

    if data not in {"none", ""}:
        add(
            "m1",
            "Domain model & data layer",
            f"The data model exists, migrates cleanly and is covered by tests ({stack.get('data', 'the chosen store')}).",
            deliverables=[
                "Schema/model definitions for the core entities",
                "Migration path that runs forward and is reversible",
                "Repository/service layer with no framework leakage",
            ],
            acceptance=[
                "Migrations apply to an empty store and to an existing one",
                "Round-trip tests prove create/read/update/delete on the core entity",
                "No SQL or ORM calls outside the data layer",
            ],
            tasks=[
                "Define the core entities and their relationships",
                "Write migrations and seed data",
                "Implement the repository/service layer",
                "Add round-trip tests for each entity",
            ],
            tools=["write_file", "apply_patch", "test_run", "shell_run", "search_code"],
            depends_on=["m0"],
            share=0.18,
            mgates=("syntax", "tests"),
            group="core",
        )

    if has_server and auth not in {"none", ""}:
        add(
            "m2",
            f"Authentication ({AXES['auth_strategy'].option(auth).label if AXES['auth_strategy'].option(auth) else auth})",
            "A user can sign in, sign out and be correctly refused when unauthorised.",
            deliverables=[
                "Session/token issuance and verification",
                "Protected-route middleware with explicit denial paths",
                "Credential storage that never logs a secret",
            ],
            acceptance=[
                "Unauthenticated access to a protected route is refused with a clear error",
                "Sign-in → protected action → sign-out works end to end",
                "Rate limiting or lockout exists on the credential endpoint",
            ],
            tasks=[
                "Implement credential verification and session issuance",
                "Protect the routes that need it",
                "Add the sign-in/sign-out UI or endpoints",
                "Test the denial paths, not just the happy path",
            ],
            tools=["write_file", "apply_patch", "test_run", "browser_flow" if has_ui else "http_fetch"],
            depends_on=["m0"] + (["m1"] if data not in {"none", ""} else []),
            share=0.14,
            mgates=("syntax", "tests"),
            group="core",
        )

    if has_ui:
        design_label = _option("design_direction", design)
        add(
            "m3",
            "Core journey UI",
            f"The primary flow is usable and looks like '{design_label.label if design_label else design}'.",
            deliverables=[
                "Design tokens (colour, spacing, type scale) matching the chosen direction",
                "The core journey implemented end to end",
                "Loading, empty and error states for every screen in the journey",
            ],
            acceptance=[
                "A first-time user completes the core journey without instruction",
                "Every async surface has a loading and an error state",
                "Layout holds at 360px and 1440px widths",
            ],
            tasks=[
                "Define the design tokens from the blueprint's design direction",
                "Build the shared layout and navigation",
                "Implement the core journey screens",
                "Add empty/loading/error states",
            ],
            tools=["write_file", "apply_patch", "browser_screenshot", "browser_flow", "test_run"],
            depends_on=[mid for mid in ("m0", "m1") if mid in {m.id for m in milestones}],
            share=0.26,
            mgates=("syntax", "build") + (("browser",) if "browser" in gates else ()),
            group="core",
        )

    integrations = blueprint.value("integrations")
    if integrations and integrations != "none":
        add(
            "m4",
            "Integrations",
            "External systems are wired with retries, timeouts and a test double for each.",
            deliverables=[
                "Client wrapper per integration with timeout and retry policy",
                "Test doubles so the suite never calls a live third party",
                "Secrets loaded from configuration, never committed",
            ],
            acceptance=[
                "Each integration fails loudly and recoverably (no silent swallow)",
                "The test suite passes with the network disabled",
            ],
            tasks=[
                "Implement the integration clients",
                "Add fakes/fixtures for tests",
                "Wire configuration and secrets",
            ],
            tools=["write_file", "http_fetch", "test_run"],
            depends_on=["m0"],
            share=0.12,
            mgates=("syntax", "tests"),
            group="extend",
        )

    add(
        "m5",
        "Verification & quality gates",
        "Everything the blueprint promised is proven by an executable check.",
        deliverables=[
            "The full gate suite green (or an honest report of what is red)",
            "Browser validation evidence where a UI exists",
            f"Priority check: {priority}",
        ],
        acceptance=[
            "Every acceptance criterion in this plan maps to a passing check",
            "No console errors and no failed requests in the browser run",
        ],
        tasks=[
            "Run the full gate suite and fix what fails",
            "Execute the browser journey and capture evidence",
            "Verify the performance/accessibility priority chosen in planning",
        ],
        tools=["run_gates", "test_run", "lint_run", "typecheck_run", "build_run", "browser_flow", "browser_console"],
        depends_on=[m.id for m in milestones],
        share=0.14,
        mgates=tuple(g for g in gates),
    )

    if maintenance not in {"throwaway", ""} or depth in {"full", "platform"}:
        add(
            "m6",
            "Docs, packaging & release",
            "Someone else can run, deploy and change this without asking.",
            deliverables=[
                "README with quickstart, commands and architecture notes",
                "Deployment runbook for the chosen target",
                "CHANGELOG entry and version bump",
            ],
            acceptance=[
                "Fresh-clone-to-running works from the README alone",
                f"Deployment to {deploy} is documented and executed (or dry-run)",
            ],
            tasks=[
                "Write the README and runbook",
                "Package/build the release artefact",
                "Record the release in CHANGELOG",
            ],
            tools=["write_file", "build_run", "git_status", "git_commit", "shell_run"],
            depends_on=["m5"],
            share=0.1,
            mgates=("syntax", "build"),
        )

    if per_test and not tdd:
        for milestone in milestones:
            if milestone.id in {"m0", "m5", "m6"}:
                continue
            milestone.tasks.append(f"Add tests for the risky paths in this milestone (target: {per_test:g} per deliverable)")

    return milestones


# --------------------------------------------------------------------------- #
# approach synthesis and scoring
# --------------------------------------------------------------------------- #
def _weights_from(blueprint: Blueprint) -> dict[str, float]:
    """Score weights derived from what the user actually said they care about."""

    weights = {key: 1.0 for key in SCORE_KEYS}
    priority = blueprint.value("performance_priority", "balanced")
    boost = {
        "speed-to-launch": {"time_to_value": 1.6, "operating_cost": 0.8},
        "ux": {"fit": 1.3, "time_to_value": 1.1},
        "seo": {"fit": 1.3, "time_to_value": 1.1},
        "throughput": {"maintainability": 1.2, "risk": 0.9},
        "cost": {"operating_cost": 1.7, "time_to_value": 1.1},
        "offline": {"fit": 1.4},
        "maintainability": {"maintainability": 1.7, "learning_curve": 1.2},
        "security": {"risk": 1.5, "maintainability": 1.2},
        "dx": {"learning_curve": 1.4, "maintainability": 1.2},
        "a11y": {"fit": 1.3},
        "conversion": {"time_to_value": 1.3, "fit": 1.2},
        "scalability": {"maintainability": 1.4, "operating_cost": 0.9},
    }.get(priority, {})
    weights.update(boost)
    if blueprint.value("scope_level") in {"hours", "days"}:
        weights["time_to_value"] *= 1.5
        weights["maintainability"] *= 0.8
    if blueprint.value("maintenance") in {"handover", "team", "oss"}:
        weights["maintainability"] *= 1.4
        weights["learning_curve"] *= 1.2
    if blueprint.value("maintenance") == "throwaway":
        weights["maintainability"] *= 0.4
        weights["time_to_value"] *= 1.6
    if blueprint.value("tech_preference") not in {"", "agent-choice"}:
        weights["fit"] *= 1.4
    return {key: round(value, 3) for key, value in weights.items()}


def score_approach(approach: Approach, weights: Mapping[str, float]) -> Approach:
    """Weighted score in 0-100; higher is better on every axis."""

    total = 0.0
    weight_sum = 0.0
    for key in SCORE_KEYS:
        weight = float(weights.get(key, 1.0))
        total += float(approach.tradeoffs.get(key, 3.0)) * weight
        weight_sum += weight
    approach.weights = dict(weights)
    approach.total = round((total / weight_sum) * 20.0, 2) if weight_sum else 0.0
    return approach


def _candidate_approaches(blueprint: Blueprint, stack: Mapping[str, str], signals: SignalSet) -> list[Approach]:
    """Generate the alternatives worth comparing.

    Always includes: the *matched* approach (what the blueprint says), a *faster*
    one, and a *more durable* one.  Deep modes add a contrarian option, because a
    plan that only ever agrees with itself is not a plan, it is a rationalisation.
    """

    kind = blueprint.value("app_type", "web-app")
    backend = blueprint.value("backend_style", "server-fullstack")
    data = blueprint.value("data_strategy", "sql")
    depth = blueprint.value("feature_depth", "mvp")
    auth = blueprint.value("auth_strategy", "none")
    approaches: list[Approach] = []

    matched = Approach(
        id="matched",
        label=f"Matched to your answers ({backend} + {stack.get('framework', 'the chosen framework')})",
        summary=(
            f"Build exactly what the blueprint describes: {stack.get('framework', 'the chosen stack')} with "
            f"{stack.get('data', 'the chosen store')}, {depth} depth"
            + (f", {AXES['auth_strategy'].option(auth).label if AXES['auth_strategy'].option(auth) else auth}" if auth not in {"", "none"} else ", no accounts")
            + "."
        ),
        stack=dict(stack),
        steps=[
            "Scaffold the chosen stack and pin dependencies",
            "Encode the blueprint as BLUEPRINT.md and config",
            "Implement the domain model and data layer",
            "Build the core journey end to end",
            "Run every gate until green, then document and ship",
        ],
        tradeoffs={"time_to_value": 3.5, "risk": 3.5, "maintainability": 4.0, "fit": 5.0, "operating_cost": 3.0, "learning_curve": 3.5},
        risks=["Assumes the answers were complete; gaps show up as rework"],
        source="heuristic",
    )
    approaches.append(matched)

    faster_backend = "static" if kind in {"static-site", "web-app"} else ("baas" if backend not in {"baas", "static"} else "static")
    faster = Approach(
        id="fastest",
        label=f"Faster path ({faster_backend.replace('-', ' ')}, smaller first cut)",
        summary=(
            "Cut to the smallest thing that proves the idea: fewer moving parts, a hosted backend or none at all, "
            "and a deliberately narrow first milestone. Reaches a usable demo sooner and defers structure."
        ),
        stack={**dict(stack), "framework": stack.get("framework", ""), "data": "None / vendor-managed" if faster_backend != "static" else "Build-time content"},
        steps=[
            "Scaffold the narrowest viable version",
            "Hardcode or vendor-manage everything that is not the core idea",
            "Ship the one journey that matters, with error states",
            "Record every shortcut in a DEBT.md so it can be paid back",
        ],
        tradeoffs={"time_to_value": 5.0, "risk": 3.0, "maintainability": 2.0, "fit": 2.8, "operating_cost": 4.5, "learning_curve": 4.5},
        risks=["Shortcuts become load-bearing if the project survives", "May not satisfy a stated quality bar"],
        source="heuristic",
    )
    approaches.append(faster)

    durable = Approach(
        id="durable",
        label="More durable (explicit boundaries, contract tests, staged delivery)",
        summary=(
            "Same product, built to be changed: module boundaries and contracts written first, "
            "tests at the seams, and delivery in stages that are each independently useful."
        ),
        stack={**dict(stack), "tests": str(stack.get("tests", "")) + " + contract tests"},
        steps=[
            "Write the domain contracts and module boundaries first",
            "Add contract and integration tests at every seam",
            "Implement in stages, each shippable on its own",
            "Document the architecture decisions as ADRs",
        ],
        tradeoffs={"time_to_value": 2.2, "risk": 4.5, "maintainability": 5.0, "fit": 3.4, "operating_cost": 3.2, "learning_curve": 3.0},
        risks=["Slower to first value; over-engineering if the project is throwaway"],
        source="heuristic",
    )
    approaches.append(durable)

    if blueprint.value("app_type") in {"web-app", "static-site", "api", "bot"}:
        approaches.append(
            Approach(
                id="boring",
                label="Boring technology (fewer dependencies, more stdlib)",
                summary=(
                    "Deliberately unexciting: the most standard framework for the language, the database everyone "
                    "knows, server-rendered pages, minimal JavaScript. Optimises for the person who maintains it in two years."
                ),
                stack={**dict(stack), "framework": _boring_framework(stack.get("language", "TypeScript")), "styling": "Plain CSS with custom properties"},
                steps=[
                    "Choose the most documented framework for the language",
                    "Server-render by default; add client code only where interaction demands it",
                    "Keep the dependency count visibly low",
                    "Document every deviation from the framework's conventions",
                ],
                tradeoffs={"time_to_value": 2.9, "risk": 4.4, "maintainability": 4.8, "fit": 2.4, "operating_cost": 4.0, "learning_curve": 4.6},
                risks=["Can feel dated next to a stated design direction", "Swaps a framework the user may already know"],
                source="heuristic",
            )
        )

    if kind in {"web-app", "api", "bot"} and data not in {"none", ""}:
        approaches.append(
            Approach(
                id="incremental",
                label="Vertical slice (one working slice before any breadth)",
                summary=(
                    "Instead of layering (schema → API → UI), build one complete thin slice end to end first, "
                    "prove the whole stack works together, then widen. Reduces integration risk to near zero early."
                ),
                stack=dict(stack),
                steps=[
                    "Pick the single most representative user journey",
                    "Build it through every layer, however thin",
                    "Verify it end to end (including the browser gate)",
                    "Widen slice by slice, refactoring as patterns emerge",
                ],
                tradeoffs={"time_to_value": 4.2, "risk": 4.4, "maintainability": 3.6, "fit": 3.8, "operating_cost": 3.2, "learning_curve": 3.8},
                risks=["Early slices get refactored; requires discipline not to gold-plate them"],
                source="heuristic",
            )
        )
    return approaches


def _boring_framework(language: str) -> str:
    return {
        "Python": "Django (batteries included, boring in the best way)",
        "TypeScript": "Remix or plain Express + server-rendered templates",
        "Go": "net/http + sqlc",
        "Rust": "Axum + sqlx",
    }.get(language, "The most documented framework for the language")


def synthesize(
    blueprint: Blueprint,
    *,
    signals: SignalSet | None = None,
    max_alternatives: int = 3,
    reasoning_mode: str = "",
    complete: bool = True,
) -> ProjectPlan:
    """Pure-Python deep planning.  Always returns a complete plan."""

    sig = signals or extract_signals(blueprint.goal)
    if complete:
        fill_defaults(blueprint, signals=sig)

    stack = _stack_for(blueprint)
    estimate, assumptions = _effort(blueprint)
    weights = _weights_from(blueprint)

    approaches = _candidate_approaches(blueprint, stack, sig)
    for approach in approaches:
        score_approach(approach, weights)
        approach.effort_days = round(estimate.likely_hours / 8.0 * {"fastest": 0.6, "durable": 1.4, "boring": 1.0, "incremental": 0.95}.get(approach.id, 1.0), 1)
    if max_alternatives and len(approaches) > max_alternatives:
        # always keep the matched approach, then the best scorers
        matched = [a for a in approaches if a.id == "matched"]
        others = sorted((a for a in approaches if a.id != "matched"), key=lambda a: -a.total)[: max(0, max_alternatives - 1)]
        approaches = matched + others

    ranked = sorted(approaches, key=lambda a: -a.total)
    if ranked:
        leader = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None
        matched = next((a for a in ranked if a.id == "matched"), None)
        priority = blueprint.value("performance_priority", "balanced").replace("-", " ")
        parts = [f"highest weighted score ({leader.total}) when you prioritise {priority}"]
        if runner is not None:
            parts.append(f"ahead of '{_short(runner.label)}' by {round(leader.total - runner.total, 1)}")
        if matched is not None and leader.id != "matched":
            parts.append(
                f"ahead of the blueprint-matched approach by {round(leader.total - matched.total, 1)}"
                + (" — enough to justify changing course" if leader.total - matched.total > TIE_BREAK_MARGIN else "")
            )
        leader.rationale = "Recommended: " + ", ".join(parts) + "."
        ranked = _apply_tie_break(ranked)
        # ``approaches`` is what the panels render, so it must carry the final
        # order: after a tie-break the leader is not the highest raw score, and a
        # list sorted by score while ``chosen`` says otherwise reads as a bug.
        approaches = ranked
        for approach in ranked:
            approach.recommended = False
        ranked[0].recommended = True
        if ranked[0].id == "matched" and leader.id != "matched":
            ranked[0].rationale = (
                f"'{_short(leader.label)}' scored marginally higher ({leader.total} vs {ranked[0].total}); "
                "not enough to overrule your answers. Shown as an alternative instead."
            )

    architecture = dict(_ARCH_BY_BACKEND.get(blueprint.value("backend_style", "server-fullstack"), _ARCH_BY_BACKEND["server-fullstack"]))
    architecture["stack"] = f"{stack['language']} · {stack['framework']} · {stack['data']}"
    architecture["verification"] = ", ".join(_GATES_BY_TYPE.get(blueprint.value("app_type", "web-app"), ["syntax", "tests"]))

    decisions = _decisions(blueprint, stack, ranked[0] if ranked else None, approaches)
    milestones = _milestones_for(blueprint, stack, estimate)
    verification = _verification_for(blueprint)
    risks = _risks_for(blueprint)
    non_goals = _non_goals(blueprint)

    return ProjectPlan(
        goal=blueprint.goal,
        blueprint=blueprint,
        approaches=approaches,
        chosen=ranked[0].id if ranked else "matched",
        architecture=architecture,
        decisions=decisions,
        milestones=milestones,
        risks=risks,
        verification=verification,
        non_goals=non_goals,
        estimate=estimate,
        warnings=consistency_warnings(blueprint),
        reasoning_mode=reasoning_mode,
        source="synthesized",
    )


def _short(label: str) -> str:
    """A approach label without its parenthetical, for prose."""

    return label.split("(")[0].strip()


#: Within this margin, the approach that implements what the user actually chose
#: wins.  A recommendation that overrules the user's own answers has to earn it.
TIE_BREAK_MARGIN = 2.5


def _apply_tie_break(ranked: list[Approach]) -> list[Approach]:
    """Prefer the blueprint-matched approach on a near tie.

    Rationale: ``fit`` is the only axis that measures "does this do what you asked
    for", and a single weighted point of it should not be enough to swap the stack
    the user picked.  Anything genuinely better wins by more than the margin.
    """

    if len(ranked) < 2:
        return ranked
    matched = next((a for a in ranked if a.id == "matched"), None)
    if matched is None:
        return ranked
    leader = ranked[0]
    if leader.id == "matched" or leader.total - matched.total > TIE_BREAK_MARGIN:
        return ranked
    return [matched] + [a for a in ranked if a.id != "matched"]


def _decisions(
    blueprint: Blueprint,
    stack: Mapping[str, str],
    chosen: Approach | None,
    approaches: Sequence[Approach] = (),
) -> list[ArchitectureDecision]:
    decisions: list[ArchitectureDecision] = []
    for axis_id in ("app_type", "backend_style", "data_strategy", "auth_strategy", "design_direction", "deployment", "testing_bar", "feature_depth"):
        answer = blueprint.answers.get(axis_id)
        if answer is None:
            continue
        axis = AXES.get(axis_id)
        option = axis.option(answer.value) if axis else None
        if option is None:
            continue
        alternatives = [other.label for other in (axis.options if axis else ()) if other.id != option.id][:3]
        why = answer.rationale or (option.implications[0] if option.implications else "chosen during clarification")
        decisions.append(
            ArchitectureDecision(
                topic=axis.category.title() + ": " + axis_id.replace("_", " "),
                decision=f"{option.label}" + (f" — {answer.detail}" if answer.detail else ""),
                rationale=why + (f" [{answer.source}]" if answer.source != "user" else " [your choice]"),
                alternatives=alternatives,
                source=answer.source,
            )
        )
    decisions.append(
        ArchitectureDecision(
            topic="Stack",
            decision=f"{stack.get('language')} · {stack.get('framework')} · {stack.get('data')} · tests: {stack.get('tests')}",
            rationale="Derived from the app type, the stated technology preference and the deployment target.",
            source="derived",
        )
    )
    if chosen is not None:
        decisions.append(
            ArchitectureDecision(
                topic="Approach",
                decision=f"{chosen.label} (score {chosen.total})",
                rationale=chosen.rationale or f"Weighted score {chosen.total}.",
                alternatives=[a.label for a in approaches if a.id != chosen.id][:4],
                source=chosen.source,
            )
        )
    return decisions


def _non_goals(blueprint: Blueprint) -> list[str]:
    labels: list[str] = []
    answer = blueprint.answers.get("non_goals")
    if answer is None:
        return ["Anything not required to complete the core journey in this milestone set"]
    if answer.value == "agent-decides" or not answer.value:
        depth = blueprint.value("feature_depth", "mvp")
        labels = [
            "Anything not needed for the core journey to be completable",
            "Admin tooling beyond what the core journey requires" if depth != "platform" else "",
            "Internationalisation" if blueprint.value("accessibility") != "strict" else "",
        ]
    else:
        axis = AXES.get("non_goals")
        for value in re.split(r"[,;+]", answer.value):
            option = axis.option(value.strip()) if axis else None
            labels.append(option.label if option else value.strip())
    if answer.detail:
        labels.append(answer.detail)
    return [label for label in labels if label]


# --------------------------------------------------------------------------- #
# model-enhanced planning
# --------------------------------------------------------------------------- #
PLANNER_SCHEMA_HINT = """Return JSON with this exact shape:
{
  "approaches": [
    {"id": "string", "label": "string", "summary": "string",
     "stack": {"language": "...", "framework": "...", "data": "..."},
     "steps": ["string"],
     "tradeoffs": {"time_to_value": 0-5, "risk": 0-5, "maintainability": 0-5,
                    "fit": 0-5, "operating_cost": 0-5, "learning_curve": 0-5},
     "risks": ["string"]}
  ],
  "critique": ["string"],
  "extra_risks": ["string"],
  "extra_verification": ["string"]
}
Every tradeoff number is 0-5 where 5 is BEST on that axis (5 = lowest risk, lowest cost)."""


class DeepPlanner:
    """Deterministic synthesis, optionally sharpened by a model.

    The model is never trusted with the whole plan: it proposes *alternatives* and
    *critique*, which are validated, scored with the same weights as the heuristic
    approaches, and merged.  Anything malformed is dropped with a warning, so a bad
    response degrades the plan's richness, never its correctness.
    """

    def __init__(self, provider: Any = None, *, settings: Any = None) -> None:
        self.provider = provider
        self.settings = settings

    async def plan(
        self,
        blueprint: Blueprint,
        *,
        reasoning: Any = None,
        max_alternatives: int = 3,
        explore: bool = True,
        constraints: str = "",
    ) -> ProjectPlan:
        base = synthesize(
            blueprint,
            max_alternatives=max_alternatives if not explore else max_alternatives + 2,
            reasoning_mode=getattr(reasoning, "mode", "") or "",
        )
        if self.provider is None or not explore:
            base.source = "synthesized"
            return base

        try:
            enrichment = await self._model_alternatives(blueprint, base, reasoning, constraints)
        except Exception as exc:  # pragma: no cover - network/provider failures
            base.warnings.append(f"model enrichment skipped: {exc}")
            base.source = "synthesized"
            return base

        if not enrichment:
            base.source = "synthesized"
            return base
        base.source = "synthesized+model"
        return enrichment

    async def _model_alternatives(
        self,
        blueprint: Blueprint,
        base: ProjectPlan,
        reasoning: Any,
        constraints: str,
    ) -> ProjectPlan | None:
        from unknown_agent.providers.base import ModelRequest
        from unknown_agent.core.models import Message

        system = (
            "You are the planning critic inside Unknown Agent. You are given a project blueprint "
            "(decisions already made with the user) and a deterministic plan. Your job is NOT to rewrite it: "
            "propose 2-3 genuinely different approaches a senior architect would consider, and critique the "
            "current one. Be concrete about tradeoffs and honest about risk. Do not propose approaches that "
            "contradict a user decision.\n" + PLANNER_SCHEMA_HINT
        )
        user = "\n".join(
            [
                "BLUEPRINT:",
                blueprint.spec_text(max_chars=2600),
                "",
                "CURRENT ARCHITECTURE:",
                json.dumps(base.architecture, indent=2),
                "CURRENT MILESTONES:",
                json.dumps([{"id": m.id, "title": m.title, "outcome": m.outcome} for m in base.milestones], indent=2),
                "CURRENT RISKS:",
                json.dumps(base.risks, indent=2),
                "",
                f"CONSTRAINTS: {constraints or 'none stated'}",
            ]
        )
        request = ModelRequest(
            messages=[Message(role="system", content=system), Message(role="user", content=user)],
            temperature=0.3,
            max_output_tokens=3000,
            json_mode=True,
            reasoning=dict(getattr(reasoning, "wire", {}) or {}),
        )
        response = await self.provider.complete(request)
        if not response.ok or not response.content:
            base.warnings.append(f"model enrichment unavailable: {response.error or 'empty response'}")
            return None

        payload = _extract_json(response.content)
        if payload is None:
            base.warnings.append("model returned non-JSON; kept the deterministic plan")
            return None

        weights = base.approaches[0].weights if base.approaches else _weights_from(blueprint)
        existing_ids = {a.id for a in base.approaches}
        added = 0
        for raw in payload.get("approaches") or []:
            if not isinstance(raw, Mapping):
                continue
            approach = _coerce_approach(raw, weights, existing_ids)
            if approach is None:
                continue
            base.approaches.append(approach)
            existing_ids.add(approach.id)
            added += 1

        for item in payload.get("extra_risks") or []:
            if isinstance(item, str) and item.strip() and item not in base.risks:
                base.risks.append(item.strip())
        for item in payload.get("extra_verification") or []:
            if isinstance(item, str) and item.strip() and item not in base.verification:
                base.verification.append(item.strip())
        for item in payload.get("critique") or []:
            if isinstance(item, str) and item.strip():
                base.warnings.append(f"planning critique: {item.strip()}")

        if added:
            for approach in base.approaches:
                approach.recommended = False
            ranked = sorted(base.approaches, key=lambda a: -a.total)
            ranked[0].recommended = True
            base.chosen = ranked[0].id
            ranked[0].rationale = (ranked[0].rationale or "") + " Re-ranked after model-proposed alternatives."
        base.risks = base.risks[:10]
        return base


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(text)
    if match:
        try:
            payload = json.loads(match.group(1))
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            payload = json.loads(text[start : end + 1])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _clamp_score(value: Any) -> float:
    try:
        return max(0.0, min(5.0, float(value)))
    except (TypeError, ValueError):
        return 3.0


def _coerce_approach(raw: Mapping[str, Any], weights: Mapping[str, float], taken: set[str]) -> Approach | None:
    label = str(raw.get("label") or "").strip()
    if not label:
        return None
    approach_id = str(raw.get("id") or "").strip().lower()
    approach_id = re.sub(r"[^a-z0-9_-]", "-", approach_id) or "model"
    candidate = approach_id
    suffix = 2
    while candidate in taken:
        candidate = f"{approach_id}-{suffix}"
        suffix += 1
    tradeoffs_raw = raw.get("tradeoffs") if isinstance(raw.get("tradeoffs"), Mapping) else {}
    tradeoffs = {key: _clamp_score(tradeofs_raw.get(key, 3.0)) for key in SCORE_KEYS}
    stack_raw = raw.get("stack") if isinstance(raw.get("stack"), Mapping) else {}
    approach = Approach(
        id=candidate,
        label=label[:120],
        summary=str(raw.get("summary") or "").strip()[:600],
        stack={str(k): str(v) for k, v in stack_raw.items()},
        steps=[str(step).strip() for step in (raw.get("steps") or []) if str(step).strip()][:8],
        tradeoffs=tradeoffs,
        risks=[str(risk).strip() for risk in (raw.get("risks") or []) if str(risk).strip()][:5],
        source="model",
    )
    return score_approach(approach, weights)


# --------------------------------------------------------------------------- #
# compilation into the executable plan the runner already understands
# --------------------------------------------------------------------------- #
def to_core_plan(project_plan: ProjectPlan, *, tool_names: Sequence[str] | None = None) -> Any:
    """Compile a :class:`ProjectPlan` into a core :class:`Plan` the runner executes.

    Milestones become plan steps; their acceptance criteria become the step's
    acceptance list; tool references are filtered to tools that actually exist so a
    plan can never reference a capability the session does not have.
    """

    from unknown_agent.core.models import Plan, PlanStep

    available = set(tool_names) if tool_names else None
    steps: list[PlanStep] = []
    deliverables: list[str] = []
    for milestone in project_plan.milestones:
        tools = [tool for tool in milestone.tools if available is None or tool in available]
        description = "\n".join(
            [milestone.outcome]
            + [f"- {task}" for task in milestone.tasks]
            + ([f"delivers: {', '.join(milestone.deliverables)}"] if milestone.deliverables else [])
        )
        steps.append(
            PlanStep(
                id=milestone.id,
                title=milestone.title,
                description=description,
                tools=tools or ["read_file", "write_file"],
                acceptance=list(milestone.acceptance) or [milestone.outcome],
                notes=f"gates: {', '.join(milestone.gates)}" if milestone.gates else "",
            )
        )
        deliverables.extend(milestone.deliverables)

    return Plan(
        goal=project_plan.goal,
        steps=steps,
        deliverables=list(dict.fromkeys(deliverables)),
        risks=list(project_plan.risks),
        verification=list(project_plan.verification),
        assumptions=[
            f"approach: {project_plan.chosen}",
            *(project_plan.estimate.assumptions if project_plan.estimate else []),
            *(f"non-goal: {item}" for item in project_plan.non_goals[:4]),
        ],
    )


def recommendation_of(project_plan: ProjectPlan) -> Approach | None:
    return project_plan.recommended
