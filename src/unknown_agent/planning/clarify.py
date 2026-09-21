"""Clarification: decide what to ask, in what order, with what recommendation.

The naive version of this feature asks ten questions in a fixed order and feels
like a tax form.  This module exists to make asking *cheap and rare*:

1. **Infer first.**  "Build a Stripe-powered shop" already answers ``app_type``,
   ``monetization`` and ``integrations``; asking them back is an insult.  Signals
   in the task text are matched against every axis's ``infer_from`` and every
   option's ``tags``, and answered with ``source="inferred"`` and a confidence.
2. **Score, don't guess.**  A recommendation is a weighted score over signal
   matches, prerequisite consistency, the chosen quality priorities and the
   effort budget implied by ``scope_level`` — and it always ships with the
   *reason*, so the user can disagree with the reasoning rather than the answer.
3. **Budget the questions.**  ``max_questions`` is a hard ceiling.  Axes are
   ranked by ``impact × ambiguity`` (how much it matters × how likely it is
   unknown) and only the top N are asked; the rest take their recommendation.
4. **Stay consistent.**  Answering ``backend_style = static`` locks out auth and
   data questions; answering ``app_type = api`` removes the design track
   entirely.  ``applies_when``/``skip_when`` are re-evaluated after every answer.
5. **Never block.**  Non-interactive sessions, ``--surprise-me`` and a user who
   keeps pressing Enter all produce a complete, reviewable blueprint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from unknown_agent.planning.axes import AXES, AXIS_ORDER, get_axis
from unknown_agent.planning.inference import is_specific_needle
from unknown_agent.planning.models import (
    SCOPE_PROFILES,
    Answer,
    Blueprint,
    DecisionAxis,
    DecisionOption,
    ScopeProfile,
)

__all__ = [
    "SignalSet",
    "Recommendation",
    "QuestionPlan",
    "PlannedQuestion",
    "extract_signals",
    "recommend",
    "recommend_all",
    "infer_answers",
    "match_preset",
    "plan_questions",
    "resolve_custom_answer",
    "underspecification_score",
    "fill_defaults",
    "consistency_warnings",
    "apply_answer",
]


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#./-]*")

#: Words that carry no information about the product and would only add noise.
_STOPWORDS = frozenset(
    """a an the and or but if then than that this these those for from with without into onto over under
    build make create want need please should could would can i we you it its my our your their there here
    app application thing stuff something anything really very just only also some many much more most
    project product system solution tool tools code coding software program website web page pages site
    new good best simple easy quick fast small large big full complete basic""".split()
)


@dataclass(slots=True)
class SignalSet:
    """What the task text told us, normalised."""

    text: str
    tokens: set[str] = field(default_factory=set)
    phrases: set[str] = field(default_factory=set)
    #: axis -> matched option ids, strongest first
    hits: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    #: quality words found ("fast", "secure", "accessible", …)
    qualities: dict[str, int] = field(default_factory=dict)
    #: technologies named explicitly
    tech: list[str] = field(default_factory=list)
    #: numbers/durations ("2 weeks", "500 users")
    magnitudes: list[str] = field(default_factory=list)
    preset: str | None = None

    def strength(self, axis: str, option_id: str) -> int:
        for candidate, score in self.hits.get(axis, []):
            if candidate == option_id:
                return score
        return 0

    def top(self, axis: str) -> tuple[str, int] | None:
        hits = self.hits.get(axis) or []
        return hits[0] if hits else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": sorted(self.tokens)[:80],
            "hits": {axis: [list(pair) for pair in pairs] for axis, pairs in self.hits.items()},
            "qualities": self.qualities,
            "tech": self.tech,
            "magnitudes": self.magnitudes,
            "preset": self.preset,
        }


#: Quality vocabulary → the quality key used in option weights.
_QUALITY_WORDS: dict[str, tuple[str, ...]] = {
    "speed-to-launch": ("fast", "quick", "quickly", "mvp", "prototype", "demo", "asap", "ship", "launch", "today", "weekend", "hackathon"),
    "ux": ("beautiful", "polished", "premium", "elegant", "delightful", "smooth", "nice", "ux", "ui", "design"),
    "seo": ("seo", "search", "ranking", "google", "organic", "discoverable"),
    "scalability": ("scale", "scalable", "millions", "high-traffic", "traffic", "concurrency", "load"),
    "cost": ("cheap", "free", "low-cost", "budget", "cost", "frugal", "hobby"),
    "offline": ("offline", "local-first", "air-gapped", "no-internet", "pwa"),
    "maintainability": ("maintainable", "clean", "readable", "simple", "long-term", "sustainable", "testable"),
    "security": ("secure", "security", "compliance", "gdpr", "hipaa", "soc2", "privacy", "private", "encrypted"),
    "dx": ("dx", "developer", "ergonomic", "cli", "scriptable", "extensible", "plugin"),
    "a11y": ("accessible", "accessibility", "a11y", "wcag", "screen-reader", "inclusive"),
    "conversion": ("conversion", "sales", "sell", "checkout", "funnel", "signup", "leads"),
    "interactivity": ("interactive", "realtime", "real-time", "live", "collaborative", "multiplayer"),
    "reach": ("mobile", "everyone", "consumer", "public", "global", "audience"),
}

_TECH_WORDS = (
    "python",
    "django",
    "flask",
    "fastapi",
    "typescript",
    "javascript",
    "node",
    "nextjs",
    "next.js",
    "remix",
    "react",
    "vue",
    "svelte",
    "angular",
    "astro",
    "go",
    "golang",
    "rust",
    "elixir",
    "ruby",
    "rails",
    "php",
    "laravel",
    "java",
    "spring",
    "kotlin",
    "swift",
    "flutter",
    "tailwind",
    "postgres",
    "postgresql",
    "mysql",
    "sqlite",
    "mongodb",
    "redis",
    "supabase",
    "firebase",
    "prisma",
    "docker",
    "kubernetes",
    "stripe",
    "graphql",
    "htmx",
    "flask",
)

_MAGNITUDE_RE = re.compile(
    r"\b(\d[\d,.]*)\s*(users?|customers?|requests?|rps|rows?|records?|gb|tb|ms|seconds?|minutes?|hours?|days?|weeks?|months?)\b",
    re.I,
)


def extract_signals(task: str, *, extra_context: str = "") -> SignalSet:
    """Pull every usable signal out of a free-text request.

    Deterministic and dependency-free on purpose: this runs before any model call,
    so an offline or unconfigured install still gets a sensible clarification flow.
    """

    combined = " ".join(part for part in (task, extra_context) if part)
    lowered = combined.lower()
    tokens = {token for token in _WORD_RE.findall(lowered) if token not in _STOPWORDS and len(token) > 2}
    phrases = {
        match.strip()
        for match in re.findall(r"[a-z0-9][a-z0-9 +#./-]{3,40}", lowered)
        if " " in match or "-" in match or "/" in match
    }

    signals = SignalSet(text=task, tokens=tokens, phrases=phrases)

    # option tags and per-axis inference vocabulary
    for axis_id in AXIS_ORDER:
        axis = AXES[axis_id]
        scored: list[tuple[str, int]] = []
        for option in axis.options:
            score = 0
            for needle in axis.infer_from.get(option.id, ()) or ():
                if needle in lowered:
                    # A phrase, or a needle naming a specific product/standard, is
                    # evidence on its own.  A single generic word is not: it needs a
                    # corroborating tag before it may answer a question.
                    score += 3 if (" " in needle or is_specific_needle(needle)) else 2
            for tag in option.tags:
                if tag in tokens:
                    score += 2
                elif tag in lowered:
                    score += 1
            if score:
                scored.append((option.id, score))
        if scored:
            scored.sort(key=lambda pair: (-pair[1], pair[0]))
            signals.hits[axis_id] = scored

    for quality, words in _QUALITY_WORDS.items():
        total = sum(lowered.count(word) for word in words)
        if total:
            signals.qualities[quality] = total

    signals.tech = [word for word in _TECH_WORDS if word in lowered]
    signals.magnitudes = [match.group(0) for match in _MAGNITUDE_RE.finditer(combined)][:6]

    preset = match_preset(signals)
    signals.preset = preset.key if preset else None
    return signals


def match_preset(signals: SignalSet | str) -> ScopeProfile | None:
    """Find the scope profile a request most resembles (``--preset auto``)."""

    if isinstance(signals, str):
        signals = extract_signals(signals)
    lowered = signals.text.lower()
    best: ScopeProfile | None = None
    best_score = 0
    for profile in SCOPE_PROFILES.values():
        score = 0
        for signal in profile.signals:
            if signal in lowered:
                score += 3 if " " in signal else 2
        # a preset whose answers match inferred axes is a better fit
        for axis_id, value in profile.answers.items():
            score += min(2, signals.strength(axis_id, value))
        if score > best_score:
            best, best_score = profile, score
    return best if best_score >= 4 else None


# --------------------------------------------------------------------------- #
# inference and recommendation
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Recommendation:
    """A scored suggestion for one axis."""

    axis: str
    option_id: str
    label: str
    score: float
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0
    alternatives: list[tuple[str, float]] = field(default_factory=list)

    @property
    def rationale(self) -> str:
        return " · ".join(self.reasons[:3]) if self.reasons else "safe default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "option_id": self.option_id,
            "label": self.label,
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "confidence": round(self.confidence, 3),
            "alternatives": [[option, round(score, 3)] for option, score in self.alternatives],
        }


def _option_available(option: DecisionOption, answers: Mapping[str, Answer]) -> bool:
    for axis_id, accepted in option.requires.items():
        current = answers.get(axis_id)
        if current is not None and current.value not in accepted:
            return False
    return True


def recommend(
    axis: DecisionAxis | str,
    *,
    signals: SignalSet | None = None,
    answers: Mapping[str, Answer] | None = None,
    presets: Mapping[str, str] | None = None,
) -> Recommendation:
    """Score every option on an axis and explain the winner."""

    resolved = axis if isinstance(axis, DecisionAxis) else get_axis(axis)
    known: Mapping[str, Answer] = answers or {}
    sig = signals or SignalSet(text="")
    preset_answers = dict(presets or {})
    if sig.preset and sig.preset in SCOPE_PROFILES:
        preset_answers = {**SCOPE_PROFILES[sig.preset].answers, **preset_answers}

    scored: list[tuple[DecisionOption, float, list[str]]] = []
    for option in resolved.options:
        if not _option_available(option, known):
            continue
        score = 0.0
        reasons: list[str] = []
        preset_scored = True

        signal_score = sig.strength(resolved.id, option.id)
        if signal_score:
            score += float(signal_score) * 1.5
            reasons.append(f"your wording points at {option.label.lower()}")

        tag_hits = [tag for tag in option.tags if tag in sig.tokens or tag in sig.text.lower()]
        if tag_hits:
            score += 0.75 * len(tag_hits)
            reasons.append("matches " + ", ".join(f"'{tag}'" for tag in tag_hits[:3]))

        preset_value = preset_answers.get(resolved.id)
        if preset_value == option.id and preset_scored:
            score += 2.0
            preset = SCOPE_PROFILES.get(sig.preset or "")
            reasons.append(f"part of the {preset.label if preset else sig.preset} profile")

        for quality, weight in option.weights.items():
            demand = sig.qualities.get(quality, 0)
            if demand and weight:
                score += weight * min(2.0, demand * 0.8)
                reasons.append(f"you asked for {quality.replace('-', ' ')}")

        # consistency with what is already decided
        for answered_axis, answer in known.items():
            chosen = AXES.get(answered_axis)
            if chosen is None:
                continue
            chosen_option = chosen.option(answer.value)
            if chosen_option is None:
                continue
            if resolved.id in chosen_option.presets and chosen_option.presets[resolved.id] == option.id:
                score += 1.25
                reasons.append(f"follows from {chosen_option.label.lower()}")
            if resolved.id in chosen_option.locks_out:
                score -= 5.0

        if option.recommended:
            score += 1.0
            if not reasons:
                reasons.append("the recommended starting point for this kind of project")

        # On axes where guessing is worse than asking (``problem``, ``design_reference``)
        # the declared default is boosted when nothing in the request speaks to it.
        if resolved.default_boost and option.id == resolved.default_option:
            evidence = signal_score + (0.75 * len(tag_hits))
            if evidence <= 0:
                score += resolved.default_boost
                reasons.append("better asked than guessed")

        # scope discipline: an ambitious option inside a small scope is a bad idea
        scope_answer = known.get("scope_level")
        if scope_answer is not None:
            scope_option = AXES["scope_level"].option(scope_answer.value)
            if scope_option is not None and option.effort > scope_option.effort + 1:
                score -= 1.5
                reasons.append(f"heavier than a {scope_option.label.lower()} scope usually allows")

        if sig.tech:
            haystack = f"{option.id} {option.label} {' '.join(option.tags)} {option.description}".lower()
            named = next((tech for tech in sig.tech if tech in haystack), None)
            if named:
                # An explicitly named technology outranks a profile default: the user
                # told us, so we do not get to overrule them with a preset.
                score += 3.0
                reasons.append(f"you named {named}")
                preset_scored = False

        score -= option.risk * 0.2
        scored.append((option, score, reasons))

    if not scored:
        fallback = resolved.default()
        return Recommendation(
            axis=resolved.id,
            option_id=fallback.id if fallback else "",
            label=fallback.label if fallback else "",
            score=0.0,
            reasons=["no option fits the current constraints"],
            confidence=0.2,
        )

    scored.sort(key=lambda row: (-row[1], row[0].id))
    winner, top_score, top_reasons = scored[0]
    runner_up = scored[1][1] if len(scored) > 1 else 0.0
    spread = top_score - runner_up
    confidence = max(0.15, min(0.97, 0.35 + 0.22 * top_score + 0.12 * spread))
    return Recommendation(
        axis=resolved.id,
        option_id=winner.id,
        label=winner.label,
        score=top_score,
        reasons=top_reasons or [f"default for {resolved.category} work"],
        confidence=confidence,
        alternatives=[(option.id, score) for option, score, _ in scored[1:4]],
    )


def recommend_all(
    *,
    signals: SignalSet,
    answers: Mapping[str, Answer] | None = None,
) -> dict[str, Recommendation]:
    """A recommendation for every axis, respecting current answers."""

    known = dict(answers or {})
    out: dict[str, Recommendation] = {}
    for axis_id in AXIS_ORDER:
        axis = AXES[axis_id]
        if not axis.applies(known):
            continue
        out[axis_id] = recommend(axis, signals=signals, answers=known)
    return out


def infer_answers(task: str, *, signals: SignalSet | None = None, threshold: int = 3) -> dict[str, Answer]:  # noqa: C901
    """Answer axes the request already answered, without asking.

    Only confident matches are taken (``threshold``), and every inferred answer
    records *why* so the review panel can show "I read this from your request"
    instead of silently assuming.
    """

    sig = signals or extract_signals(task)
    lowered = task.lower()
    answers: dict[str, Answer] = {}
    for axis_id in AXIS_ORDER:
        axis = AXES[axis_id]
        top = sig.top(axis_id)
        if not top:
            continue
        option_id, score = top
        option = axis.option(option_id)
        if option is None:
            continue
        hits = [needle for needle in axis.infer_from.get(option_id, ()) if needle in lowered]
        specific = [needle for needle in hits if is_specific_needle(needle)]
        # Where the bar sits depends on how expensive being wrong is.  A high-impact
        # axis (data model, auth, architecture) needs strong evidence; a lower-impact
        # one (hosting target, business model) may be answered by a single specific
        # mention, because it is cheap to confirm in the review and cheap to change.
        bar = threshold if axis.impact >= 4 else 2
        if score < bar or (score < threshold and not specific):
            continue
        confidence = min(0.95, 0.4 + 0.12 * score)
        if specific:
            confidence = max(confidence, 0.72)
        quoted = specific[0] if specific else (hits[0] if hits else "")
        answers[axis_id] = Answer(
            axis=axis_id,
            value=option.id,
            label=option.label,
            source="inferred",
            rationale=f"from your wording: {quoted!r}" if quoted else f"signal strength {score}",
            confidence=round(confidence, 2),
        )
    return answers


# --------------------------------------------------------------------------- #
# question planning
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PlannedQuestion:
    """One question, ready to render in any UI."""

    axis: DecisionAxis
    recommendation: Recommendation
    options: list[DecisionOption]
    priority: float
    already_answered: bool = False
    existing: Answer | None = None

    @property
    def question(self) -> str:
        return self.axis.question

    @property
    def allows_custom(self) -> bool:
        return self.axis.allow_custom

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis.id,
            "question": self.axis.question,
            "category": self.axis.category,
            "blurb": self.axis.blurb,
            "priority": round(self.priority, 3),
            "recommendation": self.recommendation.to_dict(),
            "options": [option.to_dict() for option in self.options],
            "multi": self.axis.multi,
            "allow_custom": self.axis.allow_custom,
            "already_answered": self.already_answered,
        }


@dataclass(slots=True)
class QuestionPlan:
    """What to ask, what to skip, and what to auto-resolve."""

    ask: list[PlannedQuestion] = field(default_factory=list)
    auto: list[PlannedQuestion] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    #: Applicable axes that ``auto`` mode declined to ask, with the reason.  Kept
    #: so ``ua plan questions --json`` can show *why* nothing was asked — silence
    #: without an explanation looks like a bug.
    declined: list[tuple[str, str]] = field(default_factory=list)
    preset: str | None = None
    max_questions: int = 6
    scope: str = "unknown"
    change_kind: str = ""
    underspecification: float = 0.0

    @property
    def question_count(self) -> int:
        return len(self.ask)

    def axis_ids(self) -> list[str]:
        return [question.axis.id for question in self.ask]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ask": [question.to_dict() for question in self.ask],
            "auto": [question.axis.id for question in self.auto],
            "skipped": self.skipped,
            "inferred": self.inferred,
            "declined": [{"axis": axis_id, "reason": reason} for axis_id, reason in self.declined],
            "preset": self.preset,
            "max_questions": self.max_questions,
            "scope": self.scope,
            "change_kind": self.change_kind,
            "underspecification": self.underspecification,
        }


def _priority(axis: DecisionAxis, rec: Recommendation, answered: bool) -> float:
    """impact × how-unknown-it-is × how-unsure-the-recommendation-is."""

    unknown = 1.0 if answered is False else 0.0
    return axis.impact * max(axis.ambiguity, 0.2) * (1.35 - rec.confidence) * (0.35 + 0.65 * unknown) + (0.0 if unknown else -5.0)


def plan_questions(
    task: str,
    *,
    blueprint: Blueprint | None = None,
    signals: SignalSet | None = None,
    max_questions: int = 6,
    mode: str = "auto",
    force_axes: Sequence[str] = (),
) -> QuestionPlan:
    """Build the question list for a request.

    ``mode``:
      * ``off``    → nothing is asked, everything is recommended;
      * ``auto``   → ask only what genuinely matters and is genuinely unknown;
      * ``always`` → ask the highest-consequence applicable axes, up to the ceiling
        (``max_questions`` is never exceeded; the remainder take their
        recommendation and land in ``plan.auto``).
    """

    sig = signals or extract_signals(task)
    base = blueprint if blueprint is not None else Blueprint(goal=task)
    answers: dict[str, Answer] = base.answers  # mutated in place: the caller keeps the inferences

    if not answers:
        for axis_id, answer in infer_answers(task, signals=sig).items():
            answers[axis_id] = answer
    # The preset is applied whether or not the caller brought answers: a preset is
    # what the request *shape* implies, and skipping it because four axes were
    # already inferred would leave the other sixteen to be asked about.
    if sig.preset and sig.preset in SCOPE_PROFILES:
        preset = SCOPE_PROFILES[sig.preset]
        base.scope = base.scope or preset.key
        for axis_id, value in preset.answers.items():
            if axis_id in answers:
                continue
            axis = AXES.get(axis_id)
            option = axis.option(value) if axis else None
            answers[axis_id] = Answer(
                axis=axis_id,
                value=value,
                label=option.label if option else value,
                source="preset",
                rationale=f"{preset.label} profile",
                confidence=0.6,
            )

    plan = QuestionPlan(preset=sig.preset, max_questions=max_questions)
    plan.inferred = sorted(axis for axis, answer in answers.items() if answer.source == "inferred")

    applicable: list[DecisionAxis] = []
    for axis_id in AXIS_ORDER:
        axis = AXES[axis_id]
        if not axis.applies(answers):
            plan.skipped.append(axis_id)
            continue
        applicable.append(axis)

    ranked: list[PlannedQuestion] = []
    for axis in applicable:
        rec = recommend(axis, signals=sig, answers=answers)
        existing = answers.get(axis.id)
        priority = _priority(axis, rec, answered=existing is not None)
        options = [option for option in axis.options if _option_available(option, answers)]
        ranked.append(
            PlannedQuestion(
                axis=axis,
                recommendation=rec,
                options=options or list(axis.options),
                priority=priority,
                already_answered=existing is not None,
                existing=existing,
            )
        )

    forced = {axis_id for axis_id in force_axes if axis_id in AXES}
    ranked.sort(key=lambda question: (-question.priority, AXIS_ORDER.index(question.axis.id)))

    # -- auto mode: asking is a cost, so it has to be earned ----------------
    scoring = underspecification_score(task, signals=sig)
    plan.scope = str(scoring.get("scope", "unknown"))
    plan.change_kind = str(scoring.get("change_kind", "") or "")
    plan.underspecification = float(scoring.get("score", 0.0))
    if mode == "auto":
        clarify = bool(scoring.get("recommend_clarify"))
        is_change = plan.scope in {"change", "mixed"}
        change_kind = str(scoring.get("change_kind", "") or "extension")
        kept: list[PlannedQuestion] = []
        for question in ranked:
            axis = question.axis
            if axis.id in forced or question.already_answered:
                kept.append(question)
                continue
            if not clarify:
                reason = f"request is specific enough (score {plan.underspecification:.2f}); recommendation taken"
            elif is_change and not _axis_relevant_to_change(axis.id, change_kind):
                reason = (
                    "this is a change to existing code: the repository already decided it"
                    if change_kind == "modification"
                    else "not a decision this change introduces"
                )
            elif axis.impact < 4 and question.recommendation.confidence >= 0.62:
                reason = f"low consequence and the recommendation is confident ({question.recommendation.confidence:.2f})"
            else:
                kept.append(question)
                continue
            plan.declined.append((axis.id, reason))
        ranked = kept

    # ``max_questions`` is a hard ceiling in every mode — the module contract says
    # so, and a ceiling that "always" ignores is not a ceiling.  What the mode
    # decides is *whether* to ask at all, never how many.
    budget = 0 if mode == "off" else max(0, int(max_questions))
    asked = 0
    for question in ranked:
        if question.axis.id in forced:
            question.already_answered = False
            plan.ask.append(question)
            asked += 1
            continue
        if question.already_answered:
            plan.auto.append(question)
            continue
        if asked < budget and question.priority > 0:
            plan.ask.append(question)
            asked += 1
        else:
            plan.auto.append(question)
    return plan


# --------------------------------------------------------------------------- #
# underspecification
# --------------------------------------------------------------------------- #
def underspecification_score(task: str, *, signals: SignalSet | None = None) -> dict[str, Any]:
    """How much of this request is guesswork, and where.

    Returns a 0-1 score plus the offending axes, which is what makes ``auto``
    clarify mode a *decision* rather than a vibe: a one-line "build me a website"
    scores high, "add a healthcheck endpoint to the FastAPI app" scores low.
    """

    sig = signals or extract_signals(task)
    words = len([token for token in sig.text.split() if token.strip(".,!?")])
    inferred_axes = set(infer_answers(task, signals=sig))
    scope = _change_scope(task)
    high_impact_unknown: list[str] = []
    weighted = 0.0
    total_impact = 0
    for axis_id in AXIS_ORDER:
        axis = AXES[axis_id]
        total_impact += axis.impact
        if axis_id in inferred_axes:
            continue
        if axis_id in sig.hits and sig.hits[axis_id][0][1] >= 2:
            continue
        weighted += axis.impact * axis.ambiguity
        if axis.impact >= 4:
            high_impact_unknown.append(axis_id)

    verbosity = max(0.0, min(1.0, 1.0 - words / 40.0))
    ambiguity = weighted / max(1, sum(AXES[axis_id].impact * AXES[axis_id].ambiguity for axis_id in AXIS_ORDER))
    # Underspecification is measured *relative to the work in scope*.  "Add a
    # healthcheck endpoint to the FastAPI app" leaves twelve product axes unknown
    # and is still perfectly specified, because none of them are decisions the task
    # asks anyone to make: the repo already made them.  Dampening by scope is what
    # keeps a small change from being interrogated like a greenfield build.
    ambiguity *= scope["ambiguity_weight"]
    # Evidence the request actually carried: named technologies and the axes its
    # wording answered.  Length alone is not specificity — "build me a website" is
    # four words of nothing, and a paragraph of hedging is still nothing — so both
    # are measured, and only one of them is rewarded.
    specificity = min(1.0, 0.34 * len(inferred_axes) + 0.09 * len(sig.tech))
    score = round(
        min(
            1.0,
            0.60 * ambiguity + 0.26 * verbosity + (0.10 if not sig.preset else 0.0) - 0.16 * specificity,
        ),
        3,
    )
    return {
        "score": score,
        "level": "high" if score >= 0.55 else ("medium" if score >= 0.32 else "low"),
        "words": words,
        "inferred_axes": sorted(inferred_axes),
        "high_impact_unknown": high_impact_unknown,
        "preset": sig.preset,
        "specificity": round(specificity, 3),
        "scope": scope["kind"],
        "change_kind": scope.get("sub_kind", ""),
        "recommend_clarify": (
            score >= 0.42
            or (len(high_impact_unknown) >= 5 and scope["kind"] != "change")
        ),
    }


#: Verbs that describe changing something which already exists.
_CHANGE_VERBS = (
    "fix", "repair", "debug", "patch", "update", "upgrade", "change", "modify", "tweak",
    "adjust", "remove", "delete", "rename", "move", "refactor", "optimise", "optimize",
    "improve", "extend", "add", "handle", "support", "migrate", "bump", "correct",
)
#: Verbs that describe creating something new.
_CREATE_VERBS = ("build", "create", "make", "scaffold", "develop", "design", "write", "start", "launch", "ship")
#: Nouns that mean "a new product surface", i.e. decisions someone must make.
_PRODUCT_NOUNS = (
    "app", "application", "site", "website", "product", "platform", "service", "system",
    "dashboard", "portal", "store", "shop", "game", "tool", "cli", "api", "saas", "mvp",
)


#: How each axis relates to work on an *existing* codebase.
#:
#: ``change``    — relevant to any change: it shapes how the work is done or proved.
#: ``extension`` — relevant only when the change adds capability (not when it merely
#:                 corrects or restructures what is there).
#: ``none``      — the repository already answered it; asking is noise.
#:
#: This is what stops "refactor the auth middleware" from being asked what the
#: design direction is.  A change request inherits the product decisions from the
#: code it changes.
_CHANGE_RELEVANCE: dict[str, str] = {
    "problem": "none",
    "audience": "none",
    "platform": "none",
    "scope_level": "change",
    "feature_depth": "extension",
    "design_direction": "extension",
    "design_reference": "none",
    "brand_color": "none",
    "backend_style": "none",
    "data_strategy": "extension",
    "auth_strategy": "extension",
    "integrations": "extension",
    "tech_preference": "none",
    "performance_priority": "extension",
    "accessibility": "extension",
    "testing_bar": "change",
    "deployment": "none",
    "monetization": "none",
    "maintenance": "none",
    "non_goals": "extension",
    "app_type": "none",
}

#: Verbs that add capability, as opposed to correcting or restructuring it.
_EXTENSION_VERBS = ("add", "extend", "support", "integrate", "implement", "introduce", "include", "wire")
#: Verbs that correct or restructure without adding capability.
_MODIFY_VERBS = (
    "fix", "repair", "debug", "patch", "correct", "refactor", "restructure", "clean",
    "remove", "delete", "rename", "move", "optimise", "optimize", "improve", "tweak",
    "adjust", "migrate", "upgrade", "bump", "simplify", "deduplicate",
)


def _change_kind(task: str) -> str:
    """``extension`` (adds capability) or ``modification`` (corrects/restructures)."""

    tokens = set(re.findall(r"[a-z']+", (task or "").lower()))
    extension = sum(1 for verb in _EXTENSION_VERBS if verb in tokens)
    modify = sum(1 for verb in _MODIFY_VERBS if verb in tokens)
    if extension and not modify:
        return "extension"
    if modify and not extension:
        return "modification"
    # "fix and extend", "add X and remove Y": treat as an extension, because new
    # capability is the part that carries decisions.
    return "extension" if extension else "modification"


def _axis_relevant_to_change(axis_id: str, kind: str) -> bool:
    relevance = _CHANGE_RELEVANCE.get(axis_id, "none")
    if relevance == "change":
        return True
    if relevance == "extension":
        return kind == "extension"
    return False


def _change_scope(task: str) -> dict[str, Any]:
    """Classify the request as a change to something existing, or new work.

    This is the difference between "the repo already decided your architecture" and
    "nobody has decided anything yet", and it is the single largest factor in how
    many questions are worth asking.
    """

    lowered = (task or "").lower()
    tokens = set(re.findall(r"[a-z']+", lowered))
    change_hits = sum(1 for verb in _CHANGE_VERBS if verb in tokens)
    create_hits = sum(1 for verb in _CREATE_VERBS if verb in tokens)
    product_hits = sum(1 for noun in _PRODUCT_NOUNS if noun in tokens)
    # "add a page to the marketing site" is a change even though it names a product
    # noun; "build me a marketing site" is not, even though it uses one verb.  The
    # discriminator is whether creation outranks modification.
    if create_hits and product_hits and create_hits >= change_hits:
        return {"kind": "greenfield", "sub_kind": "", "ambiguity_weight": 1.0}
    if change_hits and not (create_hits and product_hits):
        return {"kind": "change", "sub_kind": _change_kind(task), "ambiguity_weight": 0.42}
    if change_hits and create_hits:
        return {"kind": "mixed", "sub_kind": _change_kind(task), "ambiguity_weight": 0.72}
    return {"kind": "unknown", "sub_kind": "", "ambiguity_weight": 1.0}


# --------------------------------------------------------------------------- #
# answer handling
# --------------------------------------------------------------------------- #
def resolve_custom_answer(axis: DecisionAxis | str, text: str) -> Answer:
    """Turn free text into an answer: nearest option, or a genuine custom value."""

    resolved = axis if isinstance(axis, DecisionAxis) else get_axis(axis)
    raw = (text or "").strip()
    if not raw:
        rec = recommend(resolved)
        option = resolved.option(rec.option_id)
        return Answer(
            axis=resolved.id,
            value=rec.option_id,
            label=option.label if option else rec.label,
            source="default",
            rationale=rec.rationale,
            confidence=rec.confidence,
            asked=True,
        )
    lowered = raw.lower()
    exact = resolved.option(lowered)
    if exact is not None:
        return Answer(axis=resolved.id, value=exact.id, label=exact.label, source="user", asked=True)

    best: tuple[DecisionOption, int] | None = None
    for option in resolved.options:
        score = 0
        for token in _WORD_RE.findall(lowered):
            if token in option.id or token in option.label.lower() or token in option.tags:
                score += 2
            if token in option.description.lower():
                score += 1
        for tag in option.tags:
            if tag in lowered:
                score += 2
        if best is None or score > best[1]:
            best = (option, score)
    if best and best[1] >= 3:
        return Answer(
            axis=resolved.id,
            value=best[0].id,
            label=best[0].label,
            source="user",
            detail=raw,
            rationale=f"matched your description to '{best[0].label}'",
            asked=True,
        )
    return Answer(axis=resolved.id, value="custom", label=raw, source="user", custom=True, detail=raw, asked=True)


def apply_answer(blueprint: Blueprint, answer: Answer) -> list[str]:
    """Record an answer and apply its consequences to the blueprint.

    Returns the human-readable consequences that were applied, so the UI can say
    "choosing *Static only* also removed the authentication question" instead of
    quietly changing the plan.
    """

    blueprint.set(answer)
    applied: list[str] = []
    axis = AXES.get(answer.axis)
    option = axis.option(answer.value) if axis else None
    if option is None:
        return applied

    for target, value in option.presets.items():
        if target in blueprint.answers and blueprint.answers[target].source == "user":
            continue
        target_axis = AXES.get(target)
        target_option = target_axis.option(value) if target_axis else None
        blueprint.answers[target] = Answer(
            axis=target,
            value=value,
            label=target_option.label if target_option else value,
            source="inferred",
            rationale=f"follows from {option.label.lower()}",
            confidence=0.75,
        )
        applied.append(f"{target}: {target_option.label if target_option else value}")

    for locked in option.locks_out:
        if locked in blueprint.answers and blueprint.answers[locked].source != "user":
            removed = blueprint.answers.pop(locked)
            applied.append(f"dropped {locked} ({removed.display}) — not applicable")
    return applied


def fill_defaults(
    blueprint: Blueprint,
    *,
    signals: SignalSet | None = None,
    axes: Iterable[str] | None = None,
) -> Blueprint:
    """Complete every applicable axis with its recommendation."""

    sig = signals or extract_signals(blueprint.goal)
    targets = list(axes) if axes is not None else list(AXIS_ORDER)
    for axis_id in targets:
        axis = AXES.get(axis_id)
        if axis is None or not axis.applies(blueprint.answers):
            blueprint.answers.pop(axis_id, None)
            continue
        if axis_id in blueprint.answers:
            continue
        rec = recommend(axis, signals=sig, answers=blueprint.answers)
        option = axis.option(rec.option_id)
        blueprint.answers[axis_id] = Answer(
            axis=axis_id,
            value=rec.option_id,
            label=option.label if option else rec.label,
            source="default",
            rationale=rec.rationale,
            confidence=rec.confidence,
        )
        apply_answer(blueprint, blueprint.answers[axis_id])
    if not blueprint.scope:
        # A blueprint with no scope has no preset to check itself against, which
        # makes the review panel useless.  Prefer the preset the request matched;
        # otherwise take the scope the answers themselves describe.
        preset = match_preset(sig)
        if preset is not None:
            blueprint.scope = preset.key
        else:
            from unknown_agent.planning.models import SCOPE_PROFILES

            blueprint.scope = _scope_from_answers(blueprint) if SCOPE_PROFILES else "mvp-web"
    blueprint.revised_at = blueprint.revised_at or blueprint.created_at
    return blueprint


def _scope_from_answers(blueprint: Blueprint) -> str:
    """Choose a scope profile from the answers already on the blueprint.

    The profiles are not a hierarchy — they are recognisable shapes ("web MVP",
    "API service", "CLI tool").  Matching on the answers that *define* each shape
    means a completed blueprint always has a scope a human would recognise, which
    is what the review panel and the workflow generator both key off.
    """

    value = blueprint.value
    app_type = value("app_type")
    shape = {
        "api": "api-service",
        "cli-tool": "cli-tool",
        "mobile-app": "mobile-app",
        "static-site": "marketing-site",
        "library": "api-service",
        "data-pipeline": "api-service",
    }.get(app_type, "")
    if shape:
        return shape
    if value("feature_depth") == "prototype" or value("scope_level") == "hours":
        return "prototype"
    if "payments" in (blueprint.answers.get("integrations").value if blueprint.answers.get("integrations") else ""):
        return "ecommerce"
    if app_type in {"web-app", "bot", "game", "desktop-app"}:
        audience = value("audience")
        if audience in {"internal-team", "enterprise"} and app_type == "web-app":
            return "internal-tool"
        return "mvp-web"
    return "mvp-web"


def consistency_warnings(blueprint: Blueprint) -> list[str]:
    """Contradictions a human reviewer would catch — surfaced *before* execution."""

    warnings: list[str] = []
    value = blueprint.value
    if value("backend_style") == "static" and value("auth_strategy") not in {"", "none"}:
        warnings.append("A static backend cannot issue sessions — authentication needs a server or a managed provider.")
    if value("backend_style") == "static" and value("data_strategy") in {"sql", "nosql", "hybrid"}:
        warnings.append("Persistent storage was chosen with no backend to own it.")
    if value("app_type") == "static-site" and value("auth_strategy") not in {"", "none"}:
        warnings.append("A static site with accounts is really a web application.")
    if value("feature_depth") == "platform" and value("scope_level") in {"hours", "days"}:
        warnings.append("Platform depth inside a hours/days scope will not finish; narrow the scope or extend the timeline.")
    if value("testing_bar") == "none" and value("feature_depth") in {"full", "platform"}:
        warnings.append("A full product with no verification cannot be proven done — the agent will report unverified.")
    if value("performance_priority") == "seo" and value("backend_style") == "static" and value("app_type") == "web-app":
        warnings.append("SEO priority with a static backend and an interactive app implies client-side rendering; consider SSR.")
    if value("deployment") == "local-only" and value("audience") in {"consumers", "enterprise"}:
        warnings.append("A public audience cannot reach a local-only deployment.")
    if value("monetization") in {"subscription", "usage", "commerce", "freemium"} and value("auth_strategy") in {"", "none"}:
        warnings.append("Billing without accounts means no way to attribute usage to a customer.")
    if value("accessibility") == "strict" and value("design_direction") == "glass-neumorphic":
        warnings.append("Translucent surfaces make AAA contrast hard; expect to override the visual direction.")
    if value("maintenance") == "throwaway" and value("testing_bar") in {"thorough", "tdd"}:
        warnings.append("Throwaway scope with a thorough test suite contradicts itself.")
    return warnings
