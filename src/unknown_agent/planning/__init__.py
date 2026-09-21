"""Deep planning: turn a vague sentence into a plan the agent can be held to.

The problem this package solves is not "generate a to-do list".  It is that a
request like *"build me a site"* is not a specification, and an agent that
guesses silently produces the wrong product at full speed.  Planning here is
therefore a three-stage process, and every stage is inspectable:

**1. Read the request** (:mod:`~unknown_agent.planning.clarify`)
    Extract signals (verbs, surfaces, named technologies, constraints), match a
    scope preset, and score how underspecified the request is.  The score decides
    whether the agent asks questions at all.

**2. Ask about what matters** (:mod:`~unknown_agent.planning.axes`)
    Twenty-one decision axes — app type, data model, auth, monetisation, design
    direction, deployment, testing strategy — each with typed options, an
    *implication list* per option, an impact weight, and a recommended answer with
    a reason.  Questions are ordered by impact and filtered by applicability, so
    a static brochure site is never asked about webhook idempotency.

**3. Reason about the build** (:mod:`~unknown_agent.planning.deep`)
    Synthesise several distinct approaches, score them on the axes the user just
    answered (fit, risk, time-to-value, cost, maintainability, evidence), rank
    them, and emit milestones, architecture decisions and a definition of done.
    Ties are broken explicitly, and the rationale is written from the true
    ranking so it never describes the wrong runner-up.

:class:`~unknown_agent.planning.models.Blueprint` is the artefact that carries
state between the three stages, and
:func:`~unknown_agent.planning.deep.to_core_plan` converts the result into the
core :class:`~unknown_agent.core.models.Plan` the loop executes.

The UI is swappable: :class:`~unknown_agent.planning.panels.RichAskUI` drives an
interactive terminal chooser, :class:`~unknown_agent.planning.panels.HeadlessAskUI`
answers from flags/defaults for CI, and the Textual wizard reuses the same panels'
content.  Planning never imports a renderer.
"""

from __future__ import annotations

from unknown_agent.planning.axes import (
    AXES,
    AXIS_ORDER,
    CATEGORIES,
    axes_for,
    catalog_payload,
    get_axis,
)
from unknown_agent.planning.clarify import (
    PlannedQuestion,
    QuestionPlan,
    Recommendation,
    SignalSet,
    apply_answer,
    consistency_warnings,
    extract_signals,
    fill_defaults,
    infer_answers,
    match_preset,
    plan_questions,
    recommend,
    recommend_all,
    resolve_custom_answer,
    underspecification_score,
)
from unknown_agent.planning.deep import (
    SCORE_KEYS,
    SCORE_LABELS,
    Approach,
    ArchitectureDecision,
    DeepPlanner,
    EffortEstimate,
    Milestone,
    ProjectPlan,
    score_approach,
    synthesize,
    to_core_plan,
)
from unknown_agent.planning.models import (
    SCOPE_PROFILES,
    Answer,
    Blueprint,
    DecisionAxis,
    DecisionOption,
    ScopeProfile,
    option_by_id,
)
from unknown_agent.planning.panels import (
    AskResult,
    AskUI,
    HeadlessAskUI,
    RichAskUI,
    blueprint_panel,
    question_panel,
    resolve_plan,
)

__all__ = [
    # models
    "Answer",
    "Blueprint",
    "DecisionAxis",
    "DecisionOption",
    "SCOPE_PROFILES",
    "ScopeProfile",
    "option_by_id",
    # axes
    "AXES",
    "AXIS_ORDER",
    "CATEGORIES",
    "axes_for",
    "catalog_payload",
    "get_axis",
    # clarify
    "PlannedQuestion",
    "QuestionPlan",
    "Recommendation",
    "SignalSet",
    "apply_answer",
    "consistency_warnings",
    "extract_signals",
    "fill_defaults",
    "infer_answers",
    "match_preset",
    "plan_questions",
    "recommend",
    "recommend_all",
    "resolve_custom_answer",
    "underspecification_score",
    # deep
    "SCORE_KEYS",
    "SCORE_LABELS",
    "Approach",
    "ArchitectureDecision",
    "DeepPlanner",
    "EffortEstimate",
    "Milestone",
    "ProjectPlan",
    "score_approach",
    "synthesize",
    "to_core_plan",
    # panels
    "AskResult",
    "AskUI",
    "HeadlessAskUI",
    "RichAskUI",
    "blueprint_panel",
    "question_panel",
    "resolve_plan",
]
