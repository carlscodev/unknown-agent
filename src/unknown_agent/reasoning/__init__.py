"""Reasoning depth: one product ladder, many vendor realities.

Unknown Agent exposes *thinking* as a first-class control because it changes the
quality of the output more than almost any other knob.  But every vendor spells it
differently — and some do not spell it at all:

===========================  =======================================================
OpenAI (o-series)            ``reasoning_effort``: minimal | low | medium | high
OpenRouter                   ``reasoning``: {effort, max_tokens} or ``:raw`` params
Anthropic (extended thinking) ``thinking``: {type: enabled, budget_tokens}
DeepSeek R-family            no knob at all — the model always reasons
Groq / Mistral / most OSS    no knob; token budget and temperature are the levers
===========================  =======================================================

This module is the **single negotiation point**.  Callers ask for a depth
(``high``); :func:`resolve` returns what will actually happen: which native
control is used, what goes on the wire, what the planning/verification
consequences are, and — when the model cannot be told to think harder — an
explicit :attr:`ResolvedReasoning.degraded` flag plus a prompt-level scaffold so
the product gets *some* of the benefit instead of silently pretending.

Design rules
------------
1. **Never lie.**  If a model has no reasoning control, ``native_control`` is
   ``None`` and ``degraded`` is ``True``; ``ua reasoning show`` says so.
2. **Never break a request.**  Unknown vendor keys are dropped, not sent; a bad
   budget is clamped, not fatal.
3. **Depth is a policy, not a number.**  One mode moves the thinking budget *and*
   the planning depth *and* the verification strictness together, because those
   are the things a user actually means by "think harder".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "MODES",
    "MODE_ORDER",
    "MODE_ALIASES",
    "ReasoningDepth",
    "ResolvedReasoning",
    "depth_for",
    "resolve",
    "escalate",
    "deescalate",
    "supports_reasoning",
    "reasoning_family",
    "describe_modes",
    "params_for_settings",
    "wire_params",
    "scaffold_prompt",
    "normalize_mode",
    "vendor_capability",
    "openai_reasoning_params",
    "anthropic_reasoning_params",
    "ollama_reasoning_params",
]


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ReasoningDepth:
    """One rung of the ladder and everything it implies."""

    mode: str
    label: str
    index: int
    tagline: str
    #: Share of ``[reasoning].budget_tokens`` this rung may spend.
    budget_share: float
    #: Absolute floor/ceiling so a rung still means something on a small budget.
    min_thinking_tokens: int
    temperature: float
    #: Which planning depth this rung drives (see ``PlanningConfig.depth``).
    planning_depth: Literal["quick", "standard", "deep", "exhaustive"]
    alternatives: int
    verification: Literal["lenient", "balanced", "strict"]
    #: Vendor effort label, when the vendor speaks in words rather than tokens.
    effort: Literal["minimal", "low", "medium", "high"] | None
    #: Extra retries granted to self-healing at this rung.
    heal_budget: int
    #: Whether the agent must consider alternatives before choosing an approach.
    explore_alternatives: bool
    #: Whether an adversarial review pass runs over the produced plan/diff.
    adversarial_review: bool
    badge: str
    #: How the rung renders in a status line.
    style: str

    @property
    def thinking_tokens(self) -> int:
        return self.min_thinking_tokens

    def budget_from(self, ceiling: int) -> int:
        """Tokens of thinking this rung asks for, given the configured ceiling."""

        scaled = int(round(ceiling * self.budget_share))
        return max(self.min_thinking_tokens, scaled) if ceiling > 0 else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "label": self.label,
            "index": self.index,
            "tagline": self.tagline,
            "budget_share": self.budget_share,
            "min_thinking_tokens": self.min_thinking_tokens,
            "temperature": self.temperature,
            "planning_depth": self.planning_depth,
            "alternatives": self.alternatives,
            "verification": self.verification,
            "effort": self.effort,
            "heal_budget": self.heal_budget,
            "explore_alternatives": self.explore_alternatives,
            "adversarial_review": self.adversarial_review,
            "badge": self.badge,
        }


MODES: dict[str, ReasoningDepth] = {}


def _rung(**kwargs: Any) -> ReasoningDepth:
    depth = ReasoningDepth(**kwargs)
    MODES[depth.mode] = depth
    return depth


_rung(
    mode="default",
    label="Adaptive",
    index=0,
    tagline="Let the surface decide: quick for questions, deep for planning, high for code.",
    budget_share=0.35,
    min_thinking_tokens=4_000,
    temperature=0.2,
    planning_depth="standard",
    alternatives=2,
    verification="balanced",
    effort="medium",
    heal_budget=0,
    explore_alternatives=False,
    adversarial_review=False,
    badge="◐",
    style="ua.reason.medium",
)
_rung(
    mode="medium",
    label="Medium",
    index=1,
    tagline="Balanced. One pass of thought, standard planning, no alternatives.",
    budget_share=0.25,
    min_thinking_tokens=4_000,
    temperature=0.3,
    planning_depth="standard",
    alternatives=1,
    verification="balanced",
    effort="medium",
    heal_budget=0,
    explore_alternatives=False,
    adversarial_review=False,
    badge="◑",
    style="ua.reason.medium",
)
_rung(
    mode="high",
    label="High",
    index=2,
    tagline="The working default for code. Thinks before editing, verifies while editing.",
    budget_share=0.5,
    min_thinking_tokens=8_000,
    temperature=0.2,
    planning_depth="deep",
    alternatives=2,
    verification="balanced",
    effort="high",
    heal_budget=1,
    explore_alternatives=True,
    adversarial_review=False,
    badge="●",
    style="ua.reason.high",
)
_rung(
    mode="super",
    label="Super high",
    index=3,
    tagline="Deep planning with alternatives and an adversarial review of the chosen approach.",
    budget_share=0.75,
    min_thinking_tokens=16_000,
    temperature=0.2,
    planning_depth="deep",
    alternatives=3,
    verification="strict",
    effort="high",
    heal_budget=1,
    explore_alternatives=True,
    adversarial_review=True,
    badge="◆",
    style="ua.reason.deep",
)
_rung(
    mode="max",
    label="Maximum",
    index=4,
    tagline="Exhaustive. Full tradeoff matrix, strict gates, replanning on any contradiction.",
    budget_share=1.0,
    min_thinking_tokens=32_000,
    temperature=0.1,
    planning_depth="exhaustive",
    alternatives=4,
    verification="strict",
    effort="high",
    heal_budget=2,
    explore_alternatives=True,
    adversarial_review=True,
    badge="★",
    style="ua.reason.deep",
)
_rung(
    mode="ultra",
    label="Ultra code",
    index=5,
    tagline="Architecture-grade. Multiple candidate designs, risk assessment, proof-of-work before any file is written.",
    budget_share=1.0,
    min_thinking_tokens=48_000,
    temperature=0.1,
    planning_depth="exhaustive",
    alternatives=5,
    verification="strict",
    effort="high",
    heal_budget=3,
    explore_alternatives=True,
    adversarial_review=True,
    badge="✦",
    style="ua.reason.ultra",
)

MODE_ORDER: tuple[str, ...] = ("default", "medium", "high", "super", "max", "ultra")

#: What users type, mapped onto the ladder.  The prompt asked for
#: "default / medium / high / super high / maximum / ultra code"; all of those
#: spellings resolve here so nobody has to memorise the canonical key.
MODE_ALIASES: dict[str, str] = {
    "auto": "default",
    "adaptive": "default",
    "balanced": "medium",
    "med": "medium",
    "normal": "medium",
    "standard": "medium",
    "think": "high",
    "hi": "high",
    "deep": "super",
    "super-high": "super",
    "superhigh": "super",
    "super_high": "super",
    "very-high": "super",
    "veryhigh": "super",
    "xhigh": "super",
    "maximum": "max",
    "maximal": "max",
    "extreme": "max",
    "ultra-code": "ultra",
    "ultracode": "ultra",
    "ultra_code": "ultra",
    "architect": "ultra",
    "architecture": "ultra",
    "0": "medium",
    "1": "high",
    "2": "super",
    "3": "max",
    "4": "ultra",
}

#: Which rung each *surface* uses when the mode is ``default``.
SURFACE_DEFAULTS: dict[str, str] = {
    "chat": "medium",
    "ask": "medium",
    "quick": "medium",
    "code": "high",
    "coding": "high",
    "run": "high",
    "build": "high",
    "fix": "high",
    "refactor": "high",
    "review": "high",
    "plan": "super",
    "planning": "super",
    "clarify": "medium",
    "architecture": "ultra",
    "ultra": "ultra",
    "orchestrate": "super",
    "docs": "medium",
    "browser": "medium",
}


def normalize_mode(value: str | None) -> str:
    raw = (value or "").strip().lower().replace(" ", "-")
    if not raw:
        return "default"
    if raw in MODES:
        return raw
    if raw in MODE_ALIASES:
        return MODE_ALIASES[raw]
    raise KeyError(value or "")


def depth_for(mode: str | None) -> ReasoningDepth:
    return MODES[normalize_mode(mode)]


def describe_modes() -> list[dict[str, Any]]:
    """The ladder as data — feeds ``ua reasoning list``, onboarding and the TUI."""

    return [MODES[mode].to_dict() for mode in MODE_ORDER]


def escalate(mode: str | None, *, steps: int = 1) -> str:
    """Move up the ladder (used after a failed step or a blocked gate)."""

    current = depth_for(mode)
    index = min(current.index + max(0, steps), len(MODE_ORDER) - 1)
    # ``default`` is a policy, not a rung: escalating it lands on the next real rung.
    if index == 0:
        index = 1
    return MODE_ORDER[index]


def deescalate(mode: str | None, *, steps: int = 1) -> str:
    current = depth_for(mode)
    index = max(current.index - max(0, steps), 1)
    return MODE_ORDER[index]


# --------------------------------------------------------------------------- #
# capability detection
# --------------------------------------------------------------------------- #
#: Model-id signals for reasoning-capable families, newest vendors included.
_REASONING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-reasoning", re.compile(r"\b(o[134](?:-|$|[a-z]))|(gpt-5(?:[.-]|$))|(chatgpt-4o-latest)", re.I)),
    ("anthropic-thinking", re.compile(r"\bclaude[-.](?:3[.-]7|4|sonnet-4|opus-4|haiku-4)", re.I)),
    ("deepseek-reasoner", re.compile(r"\bdeepseek[-.]?(r1|r2|reasoner)\b", re.I)),
    ("grok-reasoning", re.compile(r"\bgrok-4\b|\bgrok[-.]?fast[-.]?reason", re.I)),
    ("qwen-thinking", re.compile(r"\bqwq\b|\bqwen3\b|\bqwen-?2\.5-?\w*-thinking\b", re.I)),
    ("gemini-thinking", re.compile(r"\bgemini[-.]?(2\.5|3)\b|\bthinking\b", re.I)),
    ("mistral-magistral", re.compile(r"\bmagistral\b|\bdevstral\b|\bmedium-reasoning\b", re.I)),
    ("openrouter-reasoning", re.compile(r"\breasoning\b|\b:online\b.*\bthink", re.I)),
    ("local-thinking", re.compile(r"\b(?:deepseek-r1|qwq|olmo3|gpt-oss)\b", re.I)),
)

#: Explicit non-reasoning markers that override a fuzzy match.
_NON_REASONING = re.compile(r"\b(?:mini|nano|flash-lite|haiku-3|instant|embed|whisper|tts)\b", re.I)


def reasoning_family(model: str) -> str | None:
    """Return the reasoning dialect a model id implies, or ``None``."""

    if not model:
        return None
    for family, pattern in _REASONING_PATTERNS:
        if pattern.search(model):
            return family
    return None


def supports_reasoning(
    model: str,
    *,
    explicit: bool | None = None,
    model_info: Mapping[str, Any] | None = None,
    catalog_entry: Mapping[str, Any] | None = None,
) -> bool:
    """Decide whether a model can be *told* to think harder.

    Precedence: explicit config → live model metadata (OpenRouter publishes
    ``supported_parameters`` including ``reasoning``) → catalog entry → model-id
    heuristics.  Guessing last is deliberate: a heuristic must never override
    something the vendor told us.
    """

    if explicit is not None:
        return bool(explicit)
    if model_info:
        for key in ("supports_reasoning", "reasoning"):
            if key in model_info and model_info[key] is not None:
                return bool(model_info[key])
        params = model_info.get("supported_parameters")
        if isinstance(params, Sequence) and not isinstance(params, (str, bytes)):
            if any("reasoning" in str(item).lower() for item in params):
                return True
    if catalog_entry and catalog_entry.get("supports_reasoning") is not None:
        return bool(catalog_entry["supports_reasoning"])
    if model and _NON_REASONING.search(model) and reasoning_family(model) is None:
        return False
    return reasoning_family(model) is not None


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
#: How a vendor accepts "think harder".
NativeControl = Literal[
    "reasoning_effort",
    "thinking_budget",
    "reasoning_tokens",
    "reasoning_object",
    "always_on",
    "temperature_only",
    None,
]

#: Vendors whose models reason *always* but expose no depth knob.  Sending them a
#: reasoning parameter is not "harmless" - several reject unknown keys with a 400,
#: and pretending we controlled the depth would be a lie in the status line.
_ALWAYS_ON_VENDORS = frozenset({"deepseek", "moonshot", "zhipu"})

#: Vendors whose OpenAI-compatible surface takes no reasoning parameter at all.
_NO_REASONING_PARAM_VENDORS = frozenset({"deepseek", "perplexity", "cohere", "cerebras", "azure-openai"})


def vendor_capability(vendor: str, protocol: str) -> str:
    """``effort`` | ``budget`` | ``tokens`` | ``always_on`` | ``none``."""

    key = (vendor or "").lower()
    if protocol == "anthropic":
        return "budget"
    if protocol == "ollama":
        return "tokens"
    if protocol == "offline":
        return "none"
    if key in _ALWAYS_ON_VENDORS:
        return "always_on"
    if key in _NO_REASONING_PARAM_VENDORS:
        return "none"
    return "effort"


@dataclass(slots=True)
class ResolvedReasoning:
    """What will actually happen for one request."""

    mode: str
    depth: ReasoningDepth
    surface: str
    native_control: NativeControl
    thinking_tokens: int
    effort: str | None
    wire: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    degrade_kind: str = ""
    degrade_reason: str = ""
    scaffold: str = ""
    planning_depth: str = "standard"
    alternatives: int = 1
    verification: str = "balanced"
    temperature: float = 0.2
    heal_budget: int = 0
    explore_alternatives: bool = False
    adversarial_review: bool = False
    requested_mode: str = ""
    model: str = ""
    provider: str = ""

    @property
    def label(self) -> str:
        return self.depth.label

    @property
    def badge(self) -> str:
        return self.depth.badge

    def summary(self) -> str:
        """One-line human description — used in the status bar and ``ua run``."""

        if self.native_control == "always_on":
            return f"{self.depth.badge} {self.depth.label} · always-on reasoning (no depth knob on this vendor)"
        if self.native_control is None:
            return f"{self.depth.badge} {self.depth.label} · scaffolded (vendor accepts no reasoning control)"
        control = {
            "reasoning_effort": f"effort={self.effort}",
            "thinking_budget": f"thinking={self.thinking_tokens:,} tok",
            "reasoning_tokens": f"reasoning_tokens={self.thinking_tokens:,}",
            "reasoning_object": f"reasoning={self.effort}/{self.thinking_tokens:,}",
            "temperature_only": f"temperature={self.temperature}",
            "always_on": "always-on",
        }.get(self.native_control or "", "")
        return f"{self.depth.badge} {self.depth.label} · {control} · planning {self.planning_depth}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "requested_mode": self.requested_mode,
            "label": self.depth.label,
            "surface": self.surface,
            "native_control": self.native_control,
            "thinking_tokens": self.thinking_tokens,
            "effort": self.effort,
            "wire": self.wire,
            "degraded": self.degraded,
            "degrade_kind": self.degrade_kind,
            "degrade_reason": self.degrade_reason,
            "has_scaffold": bool(self.scaffold),
            "planning_depth": self.planning_depth,
            "alternatives": self.alternatives,
            "verification": self.verification,
            "temperature": self.temperature,
            "heal_budget": self.heal_budget,
            "explore_alternatives": self.explore_alternatives,
            "adversarial_review": self.adversarial_review,
            "model": self.model,
            "provider": self.provider,
        }


#: Vendor protocol → how reasoning is expressed on the wire.
_PROTOCOL_CONTROL: dict[str, NativeControl] = {
    "openai": "reasoning_effort",
    "openai-compatible": "reasoning_effort",
    "anthropic": "thinking_budget",
    "ollama": "reasoning_tokens",
    "offline": None,
}

#: Vendors whose OpenAI-compatible surface wants a nested ``reasoning`` object
#: instead of (or in addition to) the flat ``reasoning_effort`` key.
_REASONING_OBJECT_VENDORS = frozenset({"openrouter", "groq", "nvidia", "together", "fireworks"})


def resolve(
    mode: str | None = "default",
    *,
    surface: str = "run",
    model: str = "",
    provider: str = "",
    protocol: str = "openai-compatible",
    reasoning_supported: bool | None = None,
    budget_ceiling: int = 24_000,
    effort_override: str | None = None,
    fallback: str = "scaffold",
    max_output_tokens: int = 8_000,
    temperature: float | None = None,
) -> ResolvedReasoning:
    """Negotiate a requested depth down to something the vendor can honour.

    ``surface`` selects the rung when ``mode`` is ``default`` (planning thinks
    harder than a chat question).  Everything else is capability arithmetic.
    """

    requested = normalize_mode(mode)
    effective_key = SURFACE_DEFAULTS.get(surface, "high") if requested == "default" else requested
    try:
        depth = MODES[normalize_mode(effective_key)]
    except KeyError:
        depth = MODES["high"]

    can_reason = supports_reasoning(model, explicit=reasoning_supported)
    ceiling = max(0, int(budget_ceiling))
    thinking = depth.budget_from(ceiling)
    # Never ask for more thinking than the response can hold, or vendors 400.
    if max_output_tokens:
        thinking = min(thinking, max(1_024, int(max_output_tokens * 0.9)))

    resolved = ResolvedReasoning(
        mode=depth.mode,
        depth=depth,
        surface=surface,
        native_control=None,
        thinking_tokens=thinking,
        effort=effort_override or depth.effort,
        planning_depth=depth.planning_depth,
        alternatives=depth.alternatives,
        verification=depth.verification,
        temperature=depth.temperature if temperature is None else temperature,
        heal_budget=depth.heal_budget,
        explore_alternatives=depth.explore_alternatives,
        adversarial_review=depth.adversarial_review,
        requested_mode=requested,
        model=model,
        provider=provider,
    )

    if not can_reason:
        resolved.degraded = True
        resolved.degrade_kind = "model_unsupported"
        resolved.degrade_reason = (
            f"{model or 'this model'} exposes no reasoning control"
            + (f" on {provider}" if provider else "")
            + "; depth is applied through planning, verification and a prompt scaffold instead."
        )
        resolved.native_control = "temperature_only" if protocol != "offline" else None
        resolved.wire = {"temperature": resolved.temperature}
        if fallback == "scaffold" and depth.index >= 1:
            resolved.scaffold = scaffold_prompt(depth)
        return resolved

    capability = vendor_capability(provider, protocol)
    if capability == "always_on":
        # The model reasons deeply on every request; we just cannot dial it.
        resolved.native_control = "always_on"
        resolved.degraded = True
        resolved.degrade_kind = "always_on"
        resolved.degrade_reason = (
            f"{provider or 'this provider'} reasons on every request and exposes no depth control; "
            f"'{depth.label}' is applied through planning depth and verification strictness."
        )
        if fallback == "scaffold" and depth.index >= 3:
            resolved.scaffold = scaffold_prompt(depth)
        return resolved

    if capability == "none":
        resolved.native_control = None
        resolved.degraded = True
        resolved.degrade_kind = "unsupported"
        resolved.degrade_reason = (
            f"{provider or 'this provider'} does not accept reasoning parameters; "
            f"'{depth.label}' is applied through planning depth, verification strictness and a prompt scaffold."
        )
        resolved.wire = {"temperature": resolved.temperature}
        if fallback == "scaffold" and depth.index >= 1:
            resolved.scaffold = scaffold_prompt(depth)
        return resolved

    control = {"budget": "thinking_budget", "tokens": "reasoning_tokens"}.get(capability, "reasoning_effort")
    if control == "thinking_budget":
        # Anthropic requires max_tokens > budget_tokens and a floor of 1024.
        budget = max(1024, min(thinking, max(1024, max_output_tokens - 1)))
        resolved.native_control = "thinking_budget"
        resolved.thinking_tokens = budget
        resolved.wire = {"thinking": {"type": "enabled", "budget_tokens": budget}}
        # Extended thinking rejects temperature != 1 on Anthropic's API.
        resolved.temperature = 1.0
        return resolved

    if control == "reasoning_tokens":
        resolved.native_control = "reasoning_tokens"
        resolved.wire = {"reasoning_tokens": thinking}
        return resolved

    effort = effort_override or depth.effort or "medium"
    vendor = (provider or "").lower()
    if vendor in _REASONING_OBJECT_VENDORS or protocol == "openai":
        resolved.native_control = "reasoning_object" if vendor in _REASONING_OBJECT_VENDORS else "reasoning_effort"
        resolved.wire = {"reasoning_effort": effort}
        if resolved.native_control == "reasoning_object":
            payload: dict[str, Any] = {"effort": effort}
            if thinking:
                payload["max_tokens"] = thinking
            resolved.wire["reasoning"] = payload
        return resolved

    resolved.native_control = "reasoning_effort"
    resolved.wire = {"reasoning_effort": effort}
    return resolved


def params_for_settings(
    settings: Any,
    surface: str,
    *,
    escalate_by: int = 0,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
) -> tuple[dict[str, Any], ResolvedReasoning | None]:
    """Resolve ``[reasoning]`` + the active provider into wire parameters.

    This is the single seam between the product ladder and the vendors, so every
    call site that builds a :class:`~unknown_agent.providers.base.ModelRequest`
    gets the same negotiation instead of its own guess.  Duck-typed on ``settings``
    to keep :mod:`unknown_agent.reasoning` free of a config import cycle.

    Returns ``(params, resolved)``.  ``params`` may be empty — that is the
    graceful-degradation contract, meaning this vendor has no native reasoning
    control and no scaffolding was requested.  ``resolved`` is ``None`` only when
    resolution itself failed, which must never break a run.
    """

    config = getattr(settings, "reasoning", None)
    provider = getattr(settings, "active_provider", None)
    if config is None or provider is None:
        return {}, None
    mode = config.mode_for_surface(surface) if hasattr(config, "mode_for_surface") else getattr(config, "default_mode", "default")
    steps = int(escalate_by or 0)
    if steps > 0 and bool(getattr(config, "auto_escalate", True)):
        mode = escalate(mode, steps=min(int(getattr(config, "escalation_steps", 1) or 1), steps))
    protocol = str(getattr(provider, "type", "") or "openai-compatible")
    try:
        resolved = resolve(
            mode,
            surface=surface,
            model=str(getattr(provider, "model", "") or ""),
            provider=str(getattr(provider, "name", "") or protocol),
            protocol=protocol,
            reasoning_supported=(
                None if getattr(provider, "supports_reasoning", None) is None else bool(provider.supports_reasoning)
            ),
            budget_ceiling=int(getattr(config, "budget_tokens", 24_000) or 24_000),
            fallback=str(getattr(config, "fallback", "scaffold")),
            max_output_tokens=int(max_output_tokens or getattr(provider, "max_output_tokens", 8_000) or 8_000),
            temperature=temperature if temperature is not None else getattr(provider, "temperature", None),
        )
    except Exception:  # noqa: BLE001 - reasoning must never break a run
        return {}, None
    return wire_params(resolved, protocol=protocol), resolved


def wire_params(resolved: ResolvedReasoning, *, protocol: str | None = None) -> dict[str, Any]:
    """The exact keys an adapter should merge into its request payload."""

    return dict(resolved.wire or {})


def scaffold_prompt(depth: ReasoningDepth) -> str:
    """Prompt-level thinking scaffold for models with no native reasoning control.

    This is the honest degradation path: the model cannot be *told* to think
    longer, so the product asks for the thinking explicitly and budgets output for
    it.  Depth scales how much structure is demanded.
    """

    base = (
        "Before answering, reason explicitly. Structure your response as:\n"
        "1. UNDERSTAND — restate the goal, the constraints and what 'done' means.\n"
        "2. OPTIONS — list the viable approaches you considered.\n"
        "3. DECIDE — pick one and say why the others lost.\n"
        "4. VERIFY — state how each acceptance criterion will be proven.\n"
        "Only then produce the artefact (code, plan, patch).\n"
    )
    if depth.index >= 3:
        base += (
            "\nAdditionally: enumerate at least three distinct approaches with their tradeoffs, "
            "name the riskiest assumption in the chosen one, and describe how you would detect "
            "that it is wrong within the first execution step.\n"
        )
    if depth.index >= 4:
        base += (
            "\nFinally: write a short adversarial review of your own decision — the strongest "
            "argument against it — and either refute that argument with evidence or revise the decision.\n"
        )
    return base


# --------------------------------------------------------------------------- #
# adapter helpers (imported by the provider dialects)
# --------------------------------------------------------------------------- #
def openai_reasoning_params(
    payload: dict[str, Any],
    reasoning: Mapping[str, Any] | None,
    *,
    vendor: str = "",
    drop_unknown: bool = True,
) -> dict[str, Any]:
    """Merge reasoning parameters into an OpenAI-dialect payload.

    Only keys the dialect understands are written.  ``reasoning`` (the nested
    object) is sent where the vendor documents it; ``reasoning_effort`` is sent
    where it is the flat standard.  Sending both to a vendor that knows only one
    produces a 400, so the choice is made here, once.
    """

    if not reasoning:
        return payload
    merged = dict(payload)
    effort = reasoning.get("reasoning_effort")
    nested = reasoning.get("reasoning")
    if nested and vendor.lower() in _REASONING_OBJECT_VENDORS:
        merged["reasoning"] = dict(nested)
    elif effort and not drop_unknown:
        merged["reasoning"] = {"effort": effort}
    if effort:
        merged["reasoning_effort"] = effort
    if "temperature" in reasoning and reasoning.get("temperature") is not None:
        merged.setdefault("temperature", reasoning["temperature"])
    return merged


def anthropic_reasoning_params(payload: dict[str, Any], reasoning: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge extended-thinking parameters into an Anthropic payload.

    Anthropic rejects ``temperature`` alongside ``thinking``, and requires
    ``max_tokens`` to exceed the thinking budget — both are enforced here so the
    adapter cannot produce an invalid request.
    """

    if not reasoning:
        return payload
    merged = dict(payload)
    thinking = reasoning.get("thinking")
    if not isinstance(thinking, dict):
        return merged
    merged["thinking"] = dict(thinking)
    budget = int(thinking.get("budget_tokens") or 0)
    merged.pop("temperature", None)
    if budget:
        current = int(merged.get("max_tokens") or 0)
        merged["max_tokens"] = max(current, budget + 1024)
    return merged


def ollama_reasoning_params(payload: dict[str, Any], reasoning: Mapping[str, Any] | None) -> dict[str, Any]:
    """Ollama exposes reasoning through ``options`` for models that support it."""

    if not reasoning:
        return payload
    merged = dict(payload)
    tokens = reasoning.get("reasoning_tokens")
    if tokens:
        options = dict(merged.get("options") or {})
        options["reasoning_tokens"] = int(tokens)
        merged["options"] = options
    return merged
