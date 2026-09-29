"""Dynamic model routing: pick the right brain for each specialist.

A mission is not one workload.  Decomposing an ambiguous request into a DAG is
a reasoning problem; writing the patch is a code problem; looking at a
screenshot is a vision problem; running ``pytest`` in a loop is a latency
problem.  Sending all four to the same model is how you either overpay by an
order of magnitude or under-think the only step that mattered.

This module holds the policy that maps *role + task shape* onto *model*, and it
is deliberately data-driven:

* :class:`ModelCard` describes one model — tiers, context, modality, price.
* :data:`MODEL_CARDS` is the shipped catalogue.  Users override or extend it
  from config; nothing here is hard-coded into the decision path.
* :class:`RoleRouting` states what a role *needs* rather than what it *gets*,
  so a new model with the right properties is adopted without touching code.
* :class:`ModelRouter` intersects need with what the session can actually
  reach, and explains the choice.

Two properties matter more than cleverness.  First, the router never invents
availability: a model it cannot reach is never returned, so a mission does not
die twelve steps in on a 404.  Second, every decision carries a ``rationale``
and an ordered ``fallbacks`` chain, because an orchestrator that silently
downgrades your architect to a 7B local model is worse than one that fails.

Pricing note: cards for models a vendor has not published carry
``pricing=None``.  They route perfectly well; they simply refuse to invent a
cost estimate, and :meth:`ModelRouter.estimate_cost` reports the gap instead of
guessing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from unknown_agent.observability.logging import get_logger
from unknown_agent.orchestration.roles import ROLES, Role, resolve_role_id
from unknown_agent.reasoning import deescalate, depth_for, escalate, normalize_mode

log = get_logger("orchestration.router")

__all__ = [
    "Tier",
    "Complexity",
    "Pricing",
    "ModelCard",
    "MODEL_CARDS",
    "CARDS_BY_ID",
    "card_for",
    "register_card",
    "RoleRouting",
    "ROLE_ROUTING",
    "RoutingRequest",
    "RoutingDecision",
    "RoutingError",
    "ModelRouter",
    "CostEstimate",
]


class RoutingError(RuntimeError):
    """No model could satisfy a routing request."""


# --------------------------------------------------------------------------- #
# taxonomy
# --------------------------------------------------------------------------- #
class Tier(str, Enum):
    """What a model is *for*, not who made it."""

    #: Long-horizon decomposition, contradiction resolution, acceptance design.
    FRONTIER_REASONING = "frontier-reasoning"
    #: Large-diff code synthesis with strong instruction adherence.
    FRONTIER_CODE = "frontier-code"
    #: Native image understanding — screenshots, design comps, charts.
    MULTIMODAL = "multimodal"
    #: Cheap and quick: test loops, log triage, mechanical edits.
    FAST = "fast"
    #: Runs on the user's own hardware; no per-token cost, no data egress.
    LOCAL = "local"

    @property
    def label(self) -> str:
        return {
            Tier.FRONTIER_REASONING: "Frontier reasoning",
            Tier.FRONTIER_CODE: "Frontier code",
            Tier.MULTIMODAL: "Multimodal",
            Tier.FAST: "Fast",
            Tier.LOCAL: "Local",
        }[self]


class Complexity(int, Enum):
    """How much thinking the unit of work deserves."""

    TRIVIAL = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5

    @property
    def label(self) -> str:
        return self.name.capitalize()

    @staticmethod
    def coerce(value: Complexity | int | str | None, default: Complexity) -> Complexity:
        """Accept an enum, a 1-5 int, or a name — never raise on user input."""

        if value is None:
            return default
        if isinstance(value, Complexity):
            return value
        if isinstance(value, int):
            return Complexity(min(5, max(1, value)))
        text = str(value).strip().upper()
        for member in Complexity:
            if member.name == text:
                return member
        try:
            return Complexity(min(5, max(1, int(text))))
        except ValueError:
            return default


@dataclass(frozen=True, slots=True)
class Pricing:
    """USD per million tokens, as published by the vendor."""

    input_per_mtok: float
    output_per_mtok: float
    #: Reading a cached prefix. Defaults to the usual 10% of input.
    cache_read_per_mtok: float | None = None
    #: Writing a cache entry. Defaults to the usual 125% of input.
    cache_write_per_mtok: float | None = None

    @property
    def cache_read(self) -> float:
        if self.cache_read_per_mtok is not None:
            return self.cache_read_per_mtok
        return self.input_per_mtok * 0.10

    @property
    def cache_write(self) -> float:
        if self.cache_write_per_mtok is not None:
            return self.cache_write_per_mtok
        return self.input_per_mtok * 1.25

    def to_dict(self) -> dict[str, float]:
        return {
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "cache_read_per_mtok": round(self.cache_read, 6),
            "cache_write_per_mtok": round(self.cache_write, 6),
        }


@dataclass(frozen=True, slots=True)
class ModelCard:
    """Everything the router needs to know about one model."""

    id: str
    vendor: str
    label: str
    tiers: tuple[Tier, ...]
    context_window: int
    max_output_tokens: int
    #: 1 (weakest) .. 5 (frontier).  Drives "is this good enough" decisions.
    strength: int
    #: 1 (slowest) .. 5 (fastest).  Drives latency-sensitive routing.
    speed: int
    supports_vision: bool = False
    supports_tools: bool = True
    #: Exposes a controllable internal thinking budget.
    supports_thinking: bool = False
    #: Honours prompt-prefix caching, which is what makes long missions cheap.
    supports_prompt_cache: bool = False
    #: ``None`` when the vendor has not published a tariff.
    pricing: Pricing | None = None
    #: Alternative spellings accepted by :func:`card_for`.
    aliases: tuple[str, ...] = ()
    #: True when the card is a forward declaration for an unreleased model.
    #: It only ever routes if a provider actually advertises the id.
    provisional: bool = False

    def has_tier(self, tier: Tier) -> bool:
        return tier in self.tiers

    @property
    def priced(self) -> bool:
        return self.pricing is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "vendor": self.vendor,
            "label": self.label,
            "tiers": [tier.value for tier in self.tiers],
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "strength": self.strength,
            "speed": self.speed,
            "supports_vision": self.supports_vision,
            "supports_tools": self.supports_tools,
            "supports_thinking": self.supports_thinking,
            "supports_prompt_cache": self.supports_prompt_cache,
            "pricing": self.pricing.to_dict() if self.pricing else None,
            "provisional": self.provisional,
            "aliases": list(self.aliases),
        }


# --------------------------------------------------------------------------- #
# the shipped catalogue
# --------------------------------------------------------------------------- #
def _card(**kwargs: Any) -> ModelCard:
    return ModelCard(**kwargs)


#: Released models, with vendor-published tariffs at time of writing.  Prices
#: move; ``[router.pricing]`` in config overrides any card without a code change.
_RELEASED: tuple[ModelCard, ...] = (
    _card(
        id="claude-opus-4-20250514",
        vendor="anthropic",
        label="Claude Opus 4",
        tiers=(Tier.FRONTIER_REASONING, Tier.FRONTIER_CODE, Tier.MULTIMODAL),
        context_window=200_000,
        max_output_tokens=32_000,
        strength=5,
        speed=2,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=Pricing(15.0, 75.0),
        aliases=("opus", "claude-opus-4", "opus-4"),
    ),
    _card(
        id="claude-sonnet-4-20250514",
        vendor="anthropic",
        label="Claude Sonnet 4",
        tiers=(Tier.FRONTIER_CODE, Tier.MULTIMODAL, Tier.FRONTIER_REASONING),
        context_window=200_000,
        max_output_tokens=64_000,
        strength=4,
        speed=3,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=Pricing(3.0, 15.0),
        aliases=("sonnet", "claude-sonnet-4", "sonnet-4"),
    ),
    _card(
        id="claude-3-5-haiku-20241022",
        vendor="anthropic",
        label="Claude Haiku 3.5",
        tiers=(Tier.FAST,),
        context_window=200_000,
        max_output_tokens=8_192,
        strength=2,
        speed=5,
        supports_vision=True,
        supports_prompt_cache=True,
        pricing=Pricing(0.80, 4.0),
        aliases=("haiku", "claude-haiku"),
    ),
    _card(
        id="gpt-5",
        vendor="openai",
        label="GPT-5",
        tiers=(Tier.FRONTIER_REASONING, Tier.FRONTIER_CODE, Tier.MULTIMODAL),
        context_window=400_000,
        max_output_tokens=128_000,
        strength=5,
        speed=3,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=Pricing(1.25, 10.0),
        aliases=("gpt5",),
    ),
    _card(
        id="gpt-5-mini",
        vendor="openai",
        label="GPT-5 mini",
        tiers=(Tier.FAST, Tier.MULTIMODAL),
        context_window=400_000,
        max_output_tokens=128_000,
        strength=3,
        speed=5,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=Pricing(0.25, 2.0),
        aliases=("gpt5-mini",),
    ),
    _card(
        id="o3",
        vendor="openai",
        label="OpenAI o3",
        tiers=(Tier.FRONTIER_REASONING,),
        context_window=200_000,
        max_output_tokens=100_000,
        strength=5,
        speed=1,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=Pricing(2.0, 8.0),
    ),
    _card(
        id="gemini-2.5-pro",
        vendor="google",
        label="Gemini 2.5 Pro",
        tiers=(Tier.FRONTIER_REASONING, Tier.MULTIMODAL, Tier.FRONTIER_CODE),
        context_window=1_048_576,
        max_output_tokens=65_536,
        strength=5,
        speed=3,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=Pricing(1.25, 10.0),
        aliases=("gemini-pro", "gemini"),
    ),
    _card(
        id="gemini-2.5-flash",
        vendor="google",
        label="Gemini 2.5 Flash",
        tiers=(Tier.FAST, Tier.MULTIMODAL),
        context_window=1_048_576,
        max_output_tokens=65_536,
        strength=3,
        speed=5,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=Pricing(0.30, 2.50),
        aliases=("flash",),
    ),
    _card(
        id="deepseek-reasoner",
        vendor="deepseek",
        label="DeepSeek R1",
        tiers=(Tier.FRONTIER_REASONING,),
        context_window=128_000,
        max_output_tokens=32_768,
        strength=4,
        speed=2,
        supports_thinking=True,
        pricing=Pricing(0.55, 2.19),
        aliases=("r1", "deepseek-r1"),
    ),
    _card(
        id="qwen2.5-coder:32b",
        vendor="ollama",
        label="Qwen2.5 Coder 32B (local)",
        tiers=(Tier.LOCAL, Tier.FAST),
        context_window=32_768,
        max_output_tokens=8_192,
        strength=3,
        speed=3,
        pricing=Pricing(0.0, 0.0),
        aliases=("qwen-coder", "local"),
    ),
    _card(
        id="llama3.1:70b",
        vendor="ollama",
        label="Llama 3.1 70B (local)",
        tiers=(Tier.LOCAL,),
        context_window=131_072,
        max_output_tokens=8_192,
        strength=3,
        speed=2,
        pricing=Pricing(0.0, 0.0),
    ),
)

#: Forward declarations.  These are the next-generation ids callers asked us to
#: be ready for.  They are *provisional*: no tariff is invented, and the router
#: only selects one when a provider genuinely advertises the id, so nothing here
#: can cause a mission to route into a model that does not exist.
_PROVISIONAL: tuple[ModelCard, ...] = (
    _card(
        id="claude-opus-5-5",
        vendor="anthropic",
        label="Claude Opus 5.5",
        tiers=(Tier.FRONTIER_REASONING, Tier.FRONTIER_CODE, Tier.MULTIMODAL),
        context_window=500_000,
        max_output_tokens=128_000,
        strength=5,
        speed=2,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=None,
        aliases=("opus-5.5", "opus5.5", "opus-55"),
        provisional=True,
    ),
    _card(
        id="astra-6-sol",
        vendor="astra",
        label="Astra 6 Sol",
        tiers=(Tier.FRONTIER_REASONING, Tier.FRONTIER_CODE),
        context_window=1_000_000,
        max_output_tokens=128_000,
        strength=5,
        speed=2,
        supports_vision=True,
        supports_thinking=True,
        supports_prompt_cache=True,
        pricing=None,
        aliases=("astra", "astra-6", "astra6"),
        provisional=True,
    ),
)

MODEL_CARDS: tuple[ModelCard, ...] = _RELEASED + _PROVISIONAL

CARDS_BY_ID: dict[str, ModelCard] = {}


def _index(cards: Iterable[ModelCard]) -> None:
    for card in cards:
        CARDS_BY_ID[card.id.lower()] = card
        for alias in card.aliases:
            CARDS_BY_ID.setdefault(alias.lower(), card)


_index(MODEL_CARDS)


def register_card(card: ModelCard, *, overwrite: bool = True) -> None:
    """Add or replace a card at runtime (config overrides, plugins, tests)."""

    global MODEL_CARDS
    key = card.id.lower()
    if key in CARDS_BY_ID and not overwrite:
        return
    MODEL_CARDS = (*(existing for existing in MODEL_CARDS if existing.id != card.id), card)
    CARDS_BY_ID[key] = card
    for alias in card.aliases:
        CARDS_BY_ID[alias.lower()] = card


def card_for(model_id: str) -> ModelCard | None:
    """Resolve an id, alias, or ``provider/model`` pair to a card."""

    if not model_id:
        return None
    needle = model_id.strip().lower()
    direct = CARDS_BY_ID.get(needle)
    if direct is not None:
        return direct
    # ``openrouter/anthropic/claude-opus-4`` and ``anthropic:opus`` both resolve.
    for separator in ("/", ":"):
        if separator in needle:
            tail = needle.rsplit(separator, 1)[-1]
            hit = CARDS_BY_ID.get(tail)
            if hit is not None:
                return hit
    # Last resort: longest alias that the id starts with, so dated snapshots
    # (``gpt-5-2025-11-01``) still land on the right card.
    best: ModelCard | None = None
    best_len = 0
    for key, card in CARDS_BY_ID.items():
        if needle.startswith(key) and len(key) > best_len:
            best, best_len = card, len(key)
    return best


# --------------------------------------------------------------------------- #
# what each role needs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RoleRouting:
    """The requirements of one specialist, expressed as properties."""

    role_id: str
    #: Tried in order.  The first tier with an available model wins.
    preferred_tiers: tuple[Tier, ...]
    #: A model below this strength is never chosen unless nothing else exists.
    min_strength: int = 2
    #: Hard requirement — filters the pool rather than ranking it.
    requires_vision: bool = False
    requires_tools: bool = True
    #: Bias the score toward latency instead of raw capability.
    latency_sensitive: bool = False
    #: Baseline reasoning rung; complexity escalates from here.
    base_reasoning_mode: str = "high"
    #: Roles that rewrite the tree deserve a stricter floor.
    mutating: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "preferred_tiers": [tier.value for tier in self.preferred_tiers],
            "min_strength": self.min_strength,
            "requires_vision": self.requires_vision,
            "requires_tools": self.requires_tools,
            "latency_sensitive": self.latency_sensitive,
            "base_reasoning_mode": self.base_reasoning_mode,
            "mutating": self.mutating,
            "notes": self.notes,
        }


#: Policy for every role the orchestrator ships.  The five archetypes the brief
#: calls for map onto the repository's existing role ids rather than inventing a
#: parallel vocabulary: Sovereign Conductor -> planner, Backend -> coder /
#: refactorer, Frontend -> ux-finisher, Chaos QA -> verifier /
#: browser-validator, Security Auditor -> inspector / tooling.
ROLE_ROUTING: dict[str, RoleRouting] = {
    "planner": RoleRouting(
        role_id="planner",
        preferred_tiers=(Tier.FRONTIER_REASONING, Tier.FRONTIER_CODE),
        min_strength=5,
        latency_sensitive=False,
        base_reasoning_mode="super",
        mutating=False,
        notes="Sovereign conductor: decomposition, contradiction resolution, acceptance design.",
    ),
    "inspector": RoleRouting(
        role_id="inspector",
        preferred_tiers=(Tier.FRONTIER_CODE, Tier.FRONTIER_REASONING, Tier.FAST),
        min_strength=3,
        base_reasoning_mode="medium",
        mutating=False,
        notes="Ground truth and static/security audit: wide reading, no writes.",
    ),
    "coder": RoleRouting(
        role_id="coder",
        preferred_tiers=(Tier.FRONTIER_CODE, Tier.FRONTIER_REASONING),
        min_strength=4,
        base_reasoning_mode="high",
        notes="Backend and systems specialist: large coherent diffs, async cores, data models.",
    ),
    "refactorer": RoleRouting(
        role_id="refactorer",
        preferred_tiers=(Tier.FRONTIER_CODE, Tier.FRONTIER_REASONING),
        min_strength=4,
        base_reasoning_mode="high",
        notes="Behaviour-preserving structural change; needs AST-level care.",
    ),
    "ux-finisher": RoleRouting(
        role_id="ux-finisher",
        preferred_tiers=(Tier.MULTIMODAL, Tier.FRONTIER_CODE),
        min_strength=4,
        requires_vision=True,
        base_reasoning_mode="high",
        notes="Frontend and multimodal UX: must actually see the rendered screenshot.",
    ),
    "browser-validator": RoleRouting(
        role_id="browser-validator",
        preferred_tiers=(Tier.MULTIMODAL, Tier.FAST),
        min_strength=3,
        requires_vision=True,
        latency_sensitive=True,
        base_reasoning_mode="medium",
        mutating=False,
        notes="Journey replay and visual evidence; vision is non-negotiable.",
    ),
    "verifier": RoleRouting(
        role_id="verifier",
        preferred_tiers=(Tier.FAST, Tier.FRONTIER_CODE),
        min_strength=2,
        latency_sensitive=True,
        base_reasoning_mode="medium",
        mutating=False,
        notes="Autonomous chaos and QA: tight deterministic loops, cheap per iteration.",
    ),
    "documenter": RoleRouting(
        role_id="documenter",
        preferred_tiers=(Tier.FAST, Tier.FRONTIER_CODE),
        min_strength=3,
        latency_sensitive=True,
        base_reasoning_mode="medium",
        notes="Prose over novelty; verified commands matter more than depth.",
    ),
    "releaser": RoleRouting(
        role_id="releaser",
        preferred_tiers=(Tier.FRONTIER_CODE, Tier.FRONTIER_REASONING),
        min_strength=4,
        base_reasoning_mode="high",
        notes="Irreversible actions; never route to the cheapest option.",
    ),
    "tooling": RoleRouting(
        role_id="tooling",
        preferred_tiers=(Tier.FAST, Tier.FRONTIER_CODE),
        min_strength=2,
        latency_sensitive=True,
        base_reasoning_mode="medium",
        mutating=False,
        notes="Substrate diagnosis and dependency risk review.",
    ),
}

#: Applied to any role without an explicit entry (plugins, custom roles).
_DEFAULT_ROUTING = RoleRouting(
    role_id="",
    preferred_tiers=(Tier.FRONTIER_CODE, Tier.FRONTIER_REASONING, Tier.FAST),
    min_strength=3,
    base_reasoning_mode="high",
    notes="Default policy for roles without a dedicated routing entry.",
)


def routing_for(role_id: str) -> RoleRouting:
    """Policy for a role, falling back to a sane default for unknown ids."""

    resolved = resolve_role_id(role_id) or role_id
    entry = ROLE_ROUTING.get(resolved)
    if entry is not None:
        return entry
    role: Role | None = ROLES.get(resolved)
    if role is not None:
        return replace(
            _DEFAULT_ROUTING,
            role_id=resolved,
            base_reasoning_mode=role.reasoning_mode,
            mutating=role.mutates,
        )
    return replace(_DEFAULT_ROUTING, role_id=resolved)


# --------------------------------------------------------------------------- #
# requests and decisions
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RoutingRequest:
    """One question for the router."""

    role_id: str
    complexity: Complexity = Complexity.MEDIUM
    #: Set when the unit of work carries images the model must actually read.
    needs_vision: bool = False
    #: Estimated prompt size; a card whose window cannot hold it is excluded.
    estimated_input_tokens: int = 0
    #: Caller can demand a specific model; honoured when it is reachable.
    pinned_model: str = ""
    #: Cheaper is better when the mission is on a budget.
    prefer_cheap: bool = False
    #: Never leave the machine.
    require_local: bool = False
    #: Models that already failed for this unit of work.
    exclude: tuple[str, ...] = ()
    #: Free-form marker recorded on the decision, e.g. a work item id.
    work_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "complexity": self.complexity.value,
            "needs_vision": self.needs_vision,
            "estimated_input_tokens": self.estimated_input_tokens,
            "pinned_model": self.pinned_model,
            "prefer_cheap": self.prefer_cheap,
            "require_local": self.require_local,
            "exclude": list(self.exclude),
            "work_id": self.work_id,
        }


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """What a turn is expected to cost, or why we cannot say."""

    usd: float | None
    priced: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"usd": self.usd, "priced": self.priced, "reason": self.reason}


@dataclass(slots=True)
class RoutingDecision:
    """The chosen model plus everything needed to audit the choice."""

    role_id: str
    model: ModelCard
    tier: Tier
    reasoning_mode: str
    thinking_budget_tokens: int
    #: Ordered alternatives, already filtered for availability.
    fallbacks: tuple[ModelCard, ...] = ()
    rationale: str = ""
    score: float = 0.0
    complexity: Complexity = Complexity.MEDIUM
    #: True when prompt caching should be engaged for this turn.
    use_prompt_cache: bool = False
    #: Populated when the router had to relax a requirement to find anything.
    degraded: bool = False
    degraded_reason: str = ""
    work_id: str = ""

    @property
    def model_id(self) -> str:
        return self.model.id

    @property
    def fallback_ids(self) -> list[str]:
        return [card.id for card in self.fallbacks]

    def next_fallback(self, *, exclude: Sequence[str] = ()) -> ModelCard | None:
        blocked = {item.lower() for item in exclude} | {self.model.id.lower()}
        for card in self.fallbacks:
            if card.id.lower() not in blocked:
                return card
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "model": self.model.id,
            "model_label": self.model.label,
            "vendor": self.model.vendor,
            "tier": self.tier.value,
            "reasoning_mode": self.reasoning_mode,
            "thinking_budget_tokens": self.thinking_budget_tokens,
            "fallbacks": self.fallback_ids,
            "rationale": self.rationale,
            "score": round(self.score, 3),
            "complexity": self.complexity.value,
            "use_prompt_cache": self.use_prompt_cache,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "work_id": self.work_id,
        }

    def summary(self) -> str:
        badge = f"{self.model.label} ({self.tier.label})"
        budget = self.thinking_budget_tokens
        thinking = f", thinking {budget:,}t" if budget else ""
        warn = "  [degraded]" if self.degraded else ""
        return f"{self.role_id} -> {badge}, reasoning {self.reasoning_mode}{thinking}{warn}"


# --------------------------------------------------------------------------- #
# the router
# --------------------------------------------------------------------------- #
#: A callable the host supplies to answer "can this session reach this model?".
AvailabilityProbe = Callable[[str], bool]


class ModelRouter:
    """Maps role + task shape onto a concrete, reachable model.

    ``available`` is the set of model ids this session can actually call.  Pass
    the ids your configured provider profiles expose.  When it is empty the
    router assumes every released (non-provisional) card is reachable, which is
    what makes it usable in tests and dry runs without a live provider.
    """

    def __init__(
        self,
        *,
        available: Iterable[str] = (),
        cards: Iterable[ModelCard] | None = None,
        thinking_ceiling: int = 32_000,
        probe: AvailabilityProbe | None = None,
        pricing_overrides: Mapping[str, Pricing] | None = None,
    ) -> None:
        pool = tuple(cards) if cards is not None else MODEL_CARDS
        if pricing_overrides:
            pool = tuple(
                replace(card, pricing=pricing_overrides.get(card.id, card.pricing)) for card in pool
            )
        self._cards: tuple[ModelCard, ...] = pool
        self._available: set[str] = {item.strip().lower() for item in available if item.strip()}
        self._probe = probe
        self._thinking_ceiling = max(0, thinking_ceiling)
        self._decisions: list[RoutingDecision] = []

    # -- availability ------------------------------------------------------
    def is_available(self, card: ModelCard) -> bool:
        """Reachability, resolved in the order the host can be trusted."""

        if self._probe is not None:
            return self._probe(card.id)
        if self._available:
            if card.id.lower() in self._available:
                return True
            return any(alias.lower() in self._available for alias in card.aliases)
        # No inventory supplied: released cards are assumed reachable, and a
        # provisional card never is — we will not route a mission into a model
        # nobody has confirmed exists.
        return not card.provisional

    def available_cards(self) -> list[ModelCard]:
        return [card for card in self._cards if self.is_available(card)]

    def set_available(self, models: Iterable[str]) -> None:
        self._available = {item.strip().lower() for item in models if item.strip()}

    # -- scoring -----------------------------------------------------------
    def _score(self, card: ModelCard, policy: RoleRouting, request: RoutingRequest) -> float:
        """Higher is better.  Deterministic — no randomness in orchestration."""

        score = 0.0
        # Tier fit dominates: being the right *kind* of model beats being big.
        for position, tier in enumerate(policy.preferred_tiers):
            if card.has_tier(tier):
                score += 100.0 - (position * 18.0)
                break
        else:
            score -= 40.0

        # Capability, weighted by how hard the work is.
        weight = request.complexity.value / 5.0
        score += card.strength * 12.0 * weight

        # Latency matters for tight loops and cheap roles.
        if policy.latency_sensitive:
            score += card.speed * 9.0
        else:
            score += card.speed * 2.0

        # Controllable thinking is the whole point for deep work.
        if card.supports_thinking and request.complexity >= Complexity.HIGH:
            score += 22.0
        # Caching is what makes a month-long mission affordable.
        if card.supports_prompt_cache:
            score += 12.0

        if request.needs_vision or policy.requires_vision:
            score += 30.0 if card.supports_vision else -1000.0

        if request.estimated_input_tokens and card.context_window < request.estimated_input_tokens:
            score -= 1000.0
        elif request.estimated_input_tokens:
            headroom = card.context_window / max(1, request.estimated_input_tokens)
            score += min(15.0, headroom * 2.0)

        if card.strength < policy.min_strength:
            score -= 55.0 * (policy.min_strength - card.strength)

        if request.prefer_cheap and card.pricing is not None:
            blended = card.pricing.input_per_mtok + card.pricing.output_per_mtok
            score += max(-30.0, 25.0 - blended)

        if request.require_local:
            score += 60.0 if card.has_tier(Tier.LOCAL) else -1000.0

        # A provisional card is only ever reached when the host confirmed it,
        # but even then prefer a released peer of equal standing.
        if card.provisional:
            score -= 5.0
        return score

    # -- the decision ------------------------------------------------------
    def route(self, request: RoutingRequest) -> RoutingDecision:
        """Choose a model, or raise :class:`RoutingError` if nothing fits."""

        policy = routing_for(request.role_id)
        excluded = {item.strip().lower() for item in request.exclude if item.strip()}

        pool = [
            card
            for card in self._cards
            if self.is_available(card) and card.id.lower() not in excluded
        ]
        if policy.requires_tools:
            pool = [card for card in pool if card.supports_tools]
        if not pool:
            raise RoutingError(
                f"no model is reachable for role {request.role_id!r} "
                f"(excluded={sorted(excluded) or 'none'}); configure a provider profile first"
            )

        degraded = False
        degraded_reason = ""

        # Honour a pin when we can actually reach it; say so loudly when we cannot.
        if request.pinned_model:
            pinned = card_for(request.pinned_model)
            if pinned is not None and pinned in pool:
                return self._finalise(
                    pinned, policy, request, pool, 0.0, "pinned by caller", False, ""
                )
            degraded = True
            degraded_reason = (
                f"pinned model {request.pinned_model!r} is not reachable in this session"
            )
            log.warning("router: %s", degraded_reason)

        ranked = sorted(
            ((self._score(card, policy, request), card) for card in pool),
            key=lambda pair: (-pair[0], pair[1].id),
        )
        best_score, best = ranked[0]

        # A hard requirement that nothing satisfied leaves a deeply negative
        # score. Rather than return a model that cannot do the job, relax the
        # optional parts and flag the result.
        if best_score <= -500.0:
            relaxed = replace(
                request, needs_vision=False, estimated_input_tokens=0, require_local=False
            )
            ranked = sorted(
                ((self._score(card, policy, relaxed), card) for card in pool),
                key=lambda pair: (-pair[0], pair[1].id),
            )
            best_score, best = ranked[0]
            degraded = True
            extra = "no reachable model satisfied vision/context/locality; requirements relaxed"
            degraded_reason = f"{degraded_reason}; {extra}" if degraded_reason else extra
            log.warning("router: %s for role %s", extra, request.role_id)

        if best.strength < policy.min_strength and not degraded:
            degraded = True
            degraded_reason = (
                f"best reachable model is strength {best.strength}, "
                f"role {policy.role_id} wants >= {policy.min_strength}"
            )

        fallbacks = tuple(card for _, card in ranked[1:4])
        rationale = self._explain(best, policy, request)
        return self._finalise(
            best, policy, request, pool, best_score, rationale, degraded, degraded_reason, fallbacks
        )

    def _finalise(
        self,
        card: ModelCard,
        policy: RoleRouting,
        request: RoutingRequest,
        pool: Sequence[ModelCard],
        score: float,
        rationale: str,
        degraded: bool,
        degraded_reason: str,
        fallbacks: tuple[ModelCard, ...] = (),
    ) -> RoutingDecision:
        if not fallbacks:
            others = [item for item in pool if item.id != card.id]
            ranked = sorted(
                ((self._score(item, policy, request), item) for item in others),
                key=lambda pair: (-pair[0], pair[1].id),
            )
            fallbacks = tuple(item for _, item in ranked[:3])

        mode = self.reasoning_mode_for(policy, request.complexity)
        budget = self.thinking_budget_for(card, mode) if card.supports_thinking else 0
        tier = next((tier for tier in policy.preferred_tiers if card.has_tier(tier)), card.tiers[0])

        decision = RoutingDecision(
            role_id=policy.role_id or request.role_id,
            model=card,
            tier=tier,
            reasoning_mode=mode,
            thinking_budget_tokens=budget,
            fallbacks=fallbacks,
            rationale=rationale,
            score=score,
            complexity=request.complexity,
            use_prompt_cache=card.supports_prompt_cache,
            degraded=degraded,
            degraded_reason=degraded_reason,
            work_id=request.work_id,
        )
        self._decisions.append(decision)
        log.debug("router: %s", decision.summary())
        return decision

    # -- reasoning budget --------------------------------------------------
    @staticmethod
    def reasoning_mode_for(policy: RoleRouting, complexity: Complexity) -> str:
        """Escalate the role's baseline rung by how hard the work is.

        Planning at ``CRITICAL`` should think as hard as the ladder allows; a
        trivial mechanical edit should not burn a six-figure thinking budget.
        """

        base = normalize_mode(policy.base_reasoning_mode)
        steps = {
            Complexity.TRIVIAL: -2,
            Complexity.LOW: -1,
            Complexity.MEDIUM: 0,
            Complexity.HIGH: 1,
            Complexity.CRITICAL: 2,
        }[complexity]
        if steps == 0:
            return base
        if steps > 0:
            return escalate(base, steps=steps)
        # ``escalate`` clamps negative steps to zero, so stepping *down* the
        # ladder has to go through its counterpart.
        return deescalate(base, steps=-steps)

    def thinking_budget_for(self, card: ModelCard, mode: str) -> int:
        """Tokens of internal thinking to request, clamped to the model."""

        depth = depth_for(mode)
        wanted = depth.budget_from(self._thinking_ceiling)
        # Never ask for more thinking than the model can emit in total.
        return max(0, min(wanted, max(0, card.max_output_tokens - 1024)))

    def _explain(self, card: ModelCard, policy: RoleRouting, request: RoutingRequest) -> str:
        bits: list[str] = []
        tier = next((tier for tier in policy.preferred_tiers if card.has_tier(tier)), None)
        if tier is not None:
            bits.append(f"{tier.label.lower()} tier matches {policy.role_id}")
        bits.append(f"strength {card.strength}/5, speed {card.speed}/5")
        if request.needs_vision or policy.requires_vision:
            bits.append("native vision required")
        if card.supports_thinking and request.complexity >= Complexity.HIGH:
            bits.append("controllable thinking budget")
        if card.supports_prompt_cache:
            bits.append("prompt caching keeps long missions cheap")
        if request.prefer_cheap and card.pricing is not None:
            tariff = f"${card.pricing.input_per_mtok:g}/${card.pricing.output_per_mtok:g}"
            bits.append(f"{tariff} per Mtok")
        if request.estimated_input_tokens:
            bits.append(f"{card.context_window:,} ctx holds {request.estimated_input_tokens:,} in")
        return "; ".join(bits)

    # -- cost --------------------------------------------------------------
    def estimate_cost(
        self,
        card: ModelCard,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> CostEstimate:
        """USD for a turn, or an honest refusal when the tariff is unknown."""

        pricing = card.pricing
        if pricing is None:
            return CostEstimate(
                usd=None,
                priced=False,
                reason=f"{card.label} has no published tariff; set [router.pricing] to track spend",
            )
        million = 1_000_000.0
        total = (
            (input_tokens / million) * pricing.input_per_mtok
            + (output_tokens / million) * pricing.output_per_mtok
            + (cache_read_tokens / million) * pricing.cache_read
            + (cache_write_tokens / million) * pricing.cache_write
        )
        return CostEstimate(usd=round(total, 6), priced=True)

    # -- introspection -----------------------------------------------------
    def plan_for_roles(
        self,
        role_ids: Iterable[str],
        *,
        complexity: Complexity = Complexity.MEDIUM,
    ) -> dict[str, RoutingDecision]:
        """Route a whole swarm at once — what the conductor shows on start."""

        out: dict[str, RoutingDecision] = {}
        for role_id in role_ids:
            resolved = resolve_role_id(role_id) or role_id
            try:
                out[resolved] = self.route(RoutingRequest(role_id=resolved, complexity=complexity))
            except RoutingError as exc:
                log.warning("router: cannot route %s: %s", resolved, exc)
        return out

    @property
    def decisions(self) -> list[RoutingDecision]:
        return list(self._decisions)

    def describe(self) -> dict[str, Any]:
        cards = self.available_cards()
        return {
            "available": [card.id for card in cards],
            "unreachable": [card.id for card in self._cards if card not in cards],
            "thinking_ceiling": self._thinking_ceiling,
            "roles": {role_id: policy.to_dict() for role_id, policy in ROLE_ROUTING.items()},
            "decisions": [decision.to_dict() for decision in self._decisions[-50:]],
        }


def route_for_role(
    role_id: str,
    *,
    available: Iterable[str] = (),
    complexity: Complexity | int | str | None = None,
    needs_vision: bool = False,
) -> RoutingDecision:
    """One-shot convenience wrapper for callers that do not keep a router."""

    router = ModelRouter(available=available)
    request = RoutingRequest(
        role_id=role_id,
        complexity=Complexity.coerce(complexity, Complexity.MEDIUM),
        needs_vision=needs_vision,
    )
    return router.route(request)
