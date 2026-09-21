"""The decision model behind deep planning.

When a request is underspecified ("build me an app"), the failure mode of ordinary
agents is to guess silently and generate something mediocre.  Unknown Agent does
the opposite: it knows *which* decisions a project actually hinges on, it has
curated options for each, it can recommend one with a reason, and it can ask.

Everything here is **data + pure functions**.  The same axes drive:

* the Textual clarification panel (:mod:`unknown_agent.tui.panels`),
* the Rich fallback for narrow/legacy terminals (:mod:`unknown_agent.planning.panels`),
* the non-interactive path (``--preset``, ``--answer``, CI),
* the blueprint artefact that gets persisted and reused.

That is the point: one decision model, four renderings, zero duplication.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "DecisionOption",
    "DecisionAxis",
    "Answer",
    "Blueprint",
    "ScopeProfile",
    "SCOPE_PROFILES",
    "option_by_id",
]


@dataclass(frozen=True, slots=True)
class DecisionOption:
    """One curated choice on an axis."""

    id: str
    label: str
    description: str = ""
    #: Free-text signals that make this option a good fit (matched against the task).
    tags: tuple[str, ...] = ()
    #: Consequences worth telling the user *before* they choose.
    implications: tuple[str, ...] = ()
    #: Other axes this choice makes relevant / irrelevant, and preset answers for them.
    unlocks: tuple[str, ...] = ()
    locks_out: tuple[str, ...] = ()
    presets: Mapping[str, str] = field(default_factory=dict)
    #: 0-1 weights over the qualities the recommendation engine scores.
    weights: Mapping[str, float] = field(default_factory=dict)
    #: Relative effort (1 = small, 5 = platform-scale) — feeds estimation.
    effort: int = 2
    #: Relative operating cost, used for the cost-awareness label.
    cost: int = 1
    risk: int = 1
    recommended: bool = False
    #: Show this option only when another axis has one of these answers.
    requires: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "tags": list(self.tags),
            "implications": list(self.implications),
            "unlocks": list(self.unlocks),
            "locks_out": list(self.locks_out),
            "presets": dict(self.presets),
            "weights": dict(self.weights),
            "effort": self.effort,
            "cost": self.cost,
            "risk": self.risk,
            "recommended": self.recommended,
            "requires": {k: list(v) for k, v in self.requires.items()},
        }


@dataclass(frozen=True, slots=True)
class DecisionAxis:
    """One decision the project hinges on."""

    id: str
    question: str
    category: str
    blurb: str = ""
    options: tuple[DecisionOption, ...] = ()
    multi: bool = False
    allow_custom: bool = True
    #: Render as a text field with suggestions rather than a radio list.  Used on
    #: axes where the honest answer is a sentence ("what problem does it solve?").
    prefers_text: bool = False
    #: Higher = more consequence if guessed wrong.  Drives question ordering.
    impact: int = 3
    #: How often a request like this leaves the axis unspecified (0-1).
    ambiguity: float = 0.6
    #: Ask only when these other axes have one of these answers (empty = always).
    applies_when: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Never ask when these answers are present.
    skip_when: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Signals in the task text that answer this axis without asking.
    infer_from: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    default_option: str = ""
    #: Bonus applied to ``default_option`` when the request carries no evidence for
    #: this axis at all.  Used on axes where a confident-looking guess is worse
    #: than an honest question (``problem``: "you tell me").
    default_boost: float = 0.0
    #: Shown in the blueprint review as the "so what" of this axis.
    consequence: str = ""

    def __post_init__(self) -> None:
        if not self.options and self.allow_custom is False:
            raise ValueError(f"axis {self.id!r} has no options and forbids custom answers")

    def option(self, option_id: str) -> DecisionOption | None:
        return option_by_id(self.options, option_id)

    def default(self) -> DecisionOption | None:
        if self.default_option:
            found = self.option(self.default_option)
            if found:
                return found
        for candidate in self.options:
            if candidate.recommended:
                return candidate
        return self.options[0] if self.options else None

    def applies(self, answers: Mapping[str, "Answer"]) -> bool:
        """Whether this axis is still worth asking, given what is already known."""

        for axis_id, accepted in self.skip_when.items():
            current = answers.get(axis_id)
            if current is not None and current.value in accepted:
                return False
        if not self.applies_when:
            return True
        for axis_id, accepted in self.applies_when.items():
            current = answers.get(axis_id)
            if current is None:
                # Unknown yet: keep the axis, the ordering pass will ask the
                # prerequisite first.
                continue
            if current.value not in accepted:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "category": self.category,
            "blurb": self.blurb,
            "multi": self.multi,
            "allow_custom": self.allow_custom,
            "prefers_text": self.prefers_text,
            "impact": self.impact,
            "ambiguity": self.ambiguity,
            "default_option": self.default_option,
            "consequence": self.consequence,
            "options": [option.to_dict() for option in self.options],
            "applies_when": {k: list(v) for k, v in self.applies_when.items()},
            "skip_when": {k: list(v) for k, v in self.skip_when.items()},
        }


def option_by_id(options: Iterable[DecisionOption], option_id: str) -> DecisionOption | None:
    wanted = (option_id or "").strip().lower()
    for option in options:
        if option.id == wanted or option.label.lower() == wanted:
            return option
    # tolerate prefixes: "web" -> "web-app", "spa" -> "spa"
    matches = [option for option in options if option.id.startswith(wanted) and wanted]
    return matches[0] if len(matches) == 1 else None


# --------------------------------------------------------------------------- #
# answers and the blueprint
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Answer:
    """A resolved decision, with provenance."""

    axis: str
    value: str
    label: str = ""
    source: str = "user"  # user | default | model | inferred | flag | preset
    custom: bool = False
    detail: str = ""
    #: Why this was recommended — surfaced in the review panel.
    rationale: str = ""
    confidence: float = 0.0
    asked: bool = False
    answered_at: float = field(default_factory=time.time)

    @property
    def display(self) -> str:
        return self.label or self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "value": self.value,
            "label": self.label,
            "source": self.source,
            "custom": self.custom,
            "detail": self.detail,
            "rationale": self.rationale,
            "confidence": round(self.confidence, 3),
            "asked": self.asked,
        }

    @staticmethod
    def from_dict(payload: Mapping[str, Any]) -> Answer:
        return Answer(
            axis=payload.get("axis", ""),
            value=payload.get("value", ""),
            label=payload.get("label", ""),
            source=payload.get("source", "user"),
            custom=bool(payload.get("custom", False)),
            detail=payload.get("detail", ""),
            rationale=payload.get("rationale", ""),
            confidence=float(payload.get("confidence", 0.0)),
            asked=bool(payload.get("asked", False)),
        )


@dataclass(slots=True)
class ScopeProfile:
    """A named bundle of answers — one keystroke to a coherent project shape."""

    key: str
    label: str
    description: str
    answers: Mapping[str, str]
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "answers": dict(self.answers),
            "signals": list(self.signals),
        }


@dataclass(slots=True)
class Blueprint:
    """The durable artefact of planning: what we decided, and why.

    A blueprint is not a plan.  It is the *specification* the plan is derived
    from.  Persisting it (``.unknown/blueprint.toml``) means the second run of a
    project does not re-ask what the first run already settled, and ``ua plan
    --explain`` can show the reasoning behind every step.
    """

    goal: str
    kind: str = "software"
    answers: dict[str, Answer] = field(default_factory=dict)
    summary: str = ""
    constraints: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    quality_bar: list[str] = field(default_factory=list)
    tech_preferences: list[str] = field(default_factory=list)
    scope: str = ""
    created_at: float = field(default_factory=time.time)
    revised_at: float = 0.0
    source: str = "clarify"  # clarify | preset | flags | loaded | model
    revision: int = 0

    # -- access ------------------------------------------------------------ #
    def value(self, axis: str, default: str = "") -> str:
        answer = self.answers.get(axis)
        return answer.value if answer else default

    def label(self, axis: str, default: str = "") -> str:
        answer = self.answers.get(axis)
        return (answer.display if answer else "") or default

    def values(self, axes: Sequence[str]) -> dict[str, str]:
        return {axis: self.value(axis) for axis in axes}

    def set(self, answer: Answer) -> None:
        self.answers[answer.axis] = answer

    def apply_preset(self, preset: ScopeProfile, *, source: str = "preset") -> None:
        for axis, value in preset.answers.items():
            if axis in self.answers and self.answers[axis].source == "user":
                continue  # an explicit user answer always beats a preset
            self.answers[axis] = Answer(axis=axis, value=value, label=value, source=source)

    @property
    def answered_axes(self) -> list[str]:
        return sorted(self.answers)

    @property
    def user_decided(self) -> list[str]:
        return sorted(axis for axis, answer in self.answers.items() if answer.source == "user")

    @property
    def inferred(self) -> list[str]:
        return sorted(axis for axis, answer in self.answers.items() if answer.source in {"inferred", "model", "preset", "default"})

    def coverage(self, axes: Iterable[DecisionAxis]) -> float:
        """Fraction of the *applicable* axes that are answered."""

        relevant = [axis for axis in axes if axis.applies(self.answers)]
        if not relevant:
            return 1.0
        answered = sum(1 for axis in relevant if axis.id in self.answers)
        return answered / len(relevant)

    # -- rendering --------------------------------------------------------- #
    def spec_text(self, *, max_chars: int = 4000) -> str:
        """The blueprint as prose for the model.  Deterministic and diffable."""

        lines = [f"GOAL: {self.goal}"]
        if self.scope:
            lines.append(f"SCOPE PROFILE: {self.scope}")
        if self.answers:
            lines.append("DECISIONS:")
            for axis in sorted(self.answers):
                answer = self.answers[axis]
                suffix = f" ({answer.source})" if answer.source != "user" else ""
                lines.append(f"- {axis}: {answer.display}{suffix}")
                if answer.detail:
                    lines.append(f"    detail: {answer.detail}")
        if self.tech_preferences:
            lines.append("TECHNOLOGY PREFERENCES: " + ", ".join(self.tech_preferences))
        if self.constraints:
            lines.append("CONSTRAINTS: " + "; ".join(self.constraints))
        if self.non_goals:
            lines.append("NON-GOALS: " + "; ".join(self.non_goals))
        if self.quality_bar:
            lines.append("QUALITY BAR: " + "; ".join(self.quality_bar))
        if self.risks:
            lines.append("KNOWN RISKS: " + "; ".join(self.risks))
        if self.summary:
            lines.append(f"SUMMARY: {self.summary}")
        text = "\n".join(lines)
        return text[:max_chars]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "kind": self.kind,
            "scope": self.scope,
            "summary": self.summary,
            "answers": {axis: answer.to_dict() for axis, answer in self.answers.items()},
            "constraints": self.constraints,
            "non_goals": self.non_goals,
            "risks": self.risks,
            "quality_bar": self.quality_bar,
            "tech_preferences": self.tech_preferences,
            "source": self.source,
            "revision": self.revision,
            "created_at": self.created_at,
            "revised_at": self.revised_at,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, **kwargs)

    @staticmethod
    def from_dict(payload: Mapping[str, Any]) -> Blueprint:
        answers = {
            axis: Answer.from_dict(value) for axis, value in dict(payload.get("answers") or {}).items()
        }
        return Blueprint(
            goal=payload.get("goal", ""),
            kind=payload.get("kind", "software"),
            answers=answers,
            summary=payload.get("summary", ""),
            constraints=list(payload.get("constraints") or []),
            non_goals=list(payload.get("non_goals") or []),
            risks=list(payload.get("risks") or []),
            quality_bar=list(payload.get("quality_bar") or []),
            tech_preferences=list(payload.get("tech_preferences") or []),
            scope=payload.get("scope", ""),
            created_at=float(payload.get("created_at") or time.time()),
            revised_at=float(payload.get("revised_at") or 0.0),
            source=payload.get("source", "loaded"),
            revision=int(payload.get("revision", 0)),
        )


# --------------------------------------------------------------------------- #
# scope profiles: one keystroke to a coherent shape
# --------------------------------------------------------------------------- #
SCOPE_PROFILES: dict[str, ScopeProfile] = {}


def _profile(key: str, label: str, description: str, signals: tuple[str, ...], **answers: str) -> ScopeProfile:
    profile = ScopeProfile(key=key, label=label, description=description, answers=answers, signals=signals)
    SCOPE_PROFILES[key] = profile
    return profile


_profile(
    "mvp-web",
    "Web MVP",
    "Ship a usable web product fast: server-rendered or SPA, managed auth, one database, deploy to a PaaS.",
    ("mvp", "startup", "saas", "landing", "waitlist", "prototype", "web app"),
    app_type="web-app",
    platform="web",
    design_direction="minimal-editorial",
    feature_depth="mvp",
    backend_style="server-fullstack",
    auth_strategy="managed-auth",
    data_strategy="sql",
    performance_priority="speed-to-launch",
    deployment="managed-paas",
    scope_level="mvp",
    monetization="subscription",
    maintenance="solo",
)
_profile(
    "marketing-site",
    "Marketing site",
    "A fast, accessible, SEO-honest static site with a CMS-free content model.",
    ("landing page", "marketing", "portfolio", "site", "brochure", "seo"),
    app_type="static-site",
    platform="web",
    design_direction="minimal-editorial",
    feature_depth="core",
    backend_style="static",
    auth_strategy="none",
    data_strategy="none",
    performance_priority="seo",
    deployment="managed-paas",
    scope_level="weeks-1-2",
    monetization="free",
    maintenance="handover",
    integrations="analytics",
)
_profile(
    "internal-tool",
    "Internal tool",
    "Dense, keyboard-first CRUD for a team: tables, filters, bulk actions, audit log.",
    ("internal", "admin", "dashboard", "backoffice", "ops tool", "crud"),
    app_type="web-app",
    platform="web",
    audience="internal-team",
    design_direction="dense-technical",
    feature_depth="core",
    backend_style="server-fullstack",
    auth_strategy="sso",
    data_strategy="sql",
    performance_priority="maintainability",
    deployment="container",
    scope_level="mvp",
    monetization="internal",
    maintenance="team",
    integrations="internal-apis",
)
_profile(
    "api-service",
    "API service",
    "A documented, versioned, tested HTTP API with real observability.",
    ("api", "backend", "service", "microservice", "rest", "graphql", "webhook"),
    app_type="api",
    platform="server",
    design_direction="dense-technical",
    feature_depth="core",
    backend_style="api-only",
    auth_strategy="api-keys",
    data_strategy="sql",
    performance_priority="scalability",
    deployment="container",
    scope_level="mvp",
    monetization="usage",
    maintenance="team",
)
_profile(
    "cli-tool",
    "CLI tool",
    "A polished command-line product: real flags, structured output, exit codes, docs.",
    ("cli", "command line", "terminal", "script", "devtool", "automation"),
    app_type="cli-tool",
    platform="cli",
    design_direction="dense-technical",
    feature_depth="core",
    backend_style="local",
    auth_strategy="none",
    data_strategy="local-first",
    performance_priority="dx",
    deployment="self-host",
    scope_level="weeks-1-2",
    monetization="free",
    maintenance="handover",
)
_profile(
    "mobile-app",
    "Mobile app",
    "A cross-platform mobile client with offline-first data and push notifications.",
    ("mobile", "ios", "android", "app store", "react native", "flutter"),
    app_type="mobile-app",
    platform="mobile",
    design_direction="soft-consumer",
    feature_depth="core",
    backend_style="baas",
    auth_strategy="social-oauth",
    data_strategy="local-first",
    performance_priority="ux",
    deployment="managed-paas",
    scope_level="months-1-3",
    monetization="freemium",
    maintenance="team",
    integrations="push",
)
_profile(
    "prototype",
    "Throwaway prototype",
    "Fastest path to a demo that proves one idea. No auth, no tests, no deployment story.",
    ("prototype", "demo", "poc", "hackathon", "experiment", "spike"),
    app_type="web-app",
    platform="web",
    design_direction="bold-expressive",
    feature_depth="mvp",
    backend_style="static",
    auth_strategy="none",
    data_strategy="local-first",
    performance_priority="speed-to-launch",
    deployment="managed-paas",
    scope_level="days",
    monetization="none",
    maintenance="throwaway",
)
_profile(
    "ecommerce",
    "Commerce storefront",
    "Catalogue, cart, checkout and payments with a real order model.",
    ("shop", "store", "ecommerce", "checkout", "payments", "cart", "stripe"),
    app_type="web-app",
    platform="web",
    design_direction="soft-consumer",
    feature_depth="full",
    backend_style="server-fullstack",
    auth_strategy="email-password",
    data_strategy="sql",
    performance_priority="conversion",
    deployment="managed-paas",
    scope_level="months-1-3",
    monetization="commerce",
    maintenance="team",
    integrations="payments",
)
