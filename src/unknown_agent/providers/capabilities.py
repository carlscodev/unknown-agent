"""The provider capability model: one answer to "can this model do X?".

Every subsystem that behaves differently per model - vision routing, tool
schemas, reasoning negotiation, streaming renderers, computer-use - asks this
module instead of guessing.  Resolution order, most specific wins:

1. **Profile overrides** (``[providers.<name>] supports_vision = true``) -
   the user's explicit word is final; they may be running a proxy we cannot see
   through.
2. **Live model metadata** - what the vendor's own listing said about this
   exact model id (cached by discovery).
3. **The provider catalog** - what we know about the vendor as a whole.
4. **Model-id heuristics** - conservative name-based inference (``gpt-4o``,
   ``claude-*``, ``*-vl``…), used only when nothing better exists.

Every resolved capability carries its *source*, so ``ua models capabilities``
can explain itself, and the honest default for an unknown model is ``False``
for anything that would silently break (vision, computer use) and ``True`` only
for text generation itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from unknown_agent.config.models import ProviderConfig

__all__ = ["Capability", "ModelCapabilities", "resolve_capabilities", "VISION_MODEL_HINTS"]


#: Model-id fragments that indicate vision support, per family.  Heuristics are
#: the *last* resort and biased to false negatives: a missed vision model shows
#: a clear "not vision-capable" message with the override hint, while a false
#: positive would send images to a blind model and fail obscurely.
VISION_MODEL_HINTS: tuple[str, ...] = (
    "gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4",
    "claude-3", "claude-4", "sonnet", "opus", "haiku",
    "gemini",
    "llava", "-vl", "vl-", "vision",
    "pixtral", "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "minicpm-v", "moondream",
    "llama-3.2-11b", "llama-3.2-90b", "llama-4",
)

_COMPUTER_USE_HINTS: tuple[str, ...] = (
    "claude-3-5-sonnet", "claude-3-7-sonnet", "claude-4", "claude-sonnet-4", "claude-opus-4",
    "computer-use",
)


@dataclass(slots=True)
class Capability:
    """One yes/no capability plus where the answer came from."""

    value: bool
    source: str  # "profile" | "model-metadata" | "catalog" | "heuristic" | "default"

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source}


@dataclass(slots=True)
class ModelCapabilities:
    """The full capability sheet for one (provider profile, model) pair."""

    provider: str
    model: str
    tools: Capability = field(default_factory=lambda: Capability(True, "default"))
    vision: Capability = field(default_factory=lambda: Capability(False, "default"))
    reasoning: Capability = field(default_factory=lambda: Capability(False, "default"))
    streaming: Capability = field(default_factory=lambda: Capability(True, "default"))
    json_mode: Capability = field(default_factory=lambda: Capability(False, "default"))
    computer_use: Capability = field(default_factory=lambda: Capability(False, "default"))
    #: Non-boolean facts that matter for routing.
    context_window: int | None = None
    max_output_tokens: int | None = None
    limitations: list[str] = field(default_factory=list)

    # -- convenience truth accessors ----------------------------------------
    @property
    def supports_vision(self) -> bool:
        return self.vision.value

    @property
    def supports_tools(self) -> bool:
        return self.tools.value

    @property
    def supports_computer_use(self) -> bool:
        return self.computer_use.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tools": self.tools.to_dict(),
            "vision": self.vision.to_dict(),
            "reasoning": self.reasoning.to_dict(),
            "streaming": self.streaming.to_dict(),
            "json_mode": self.json_mode.to_dict(),
            "computer_use": self.computer_use.to_dict(),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "limitations": list(self.limitations),
        }

    def summary(self) -> str:
        parts = []
        for label, cap in (("tools", self.tools), ("vision", self.vision), ("reasoning", self.reasoning), ("streaming", self.streaming), ("computer-use", self.computer_use)):
            parts.append(f"{label}:{'yes' if cap.value else 'no'}")
        return "  ".join(parts)


def _heuristic_vision(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(hint in lowered for hint in VISION_MODEL_HINTS)


def _heuristic_computer_use(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(hint in lowered for hint in _COMPUTER_USE_HINTS)


def resolve_capabilities(
    config: ProviderConfig,
    *,
    provider_name: str = "default",
    model: str | None = None,
    model_info: Any = None,
) -> ModelCapabilities:
    """Resolve the capability sheet for a profile (optionally a specific model).

    ``model_info`` is a :class:`~unknown_agent.providers.discovery.ModelInfo`
    when the caller has live metadata; passing it upgrades the answer's source
    from catalog/heuristic to model-metadata.
    """

    from unknown_agent.providers.catalog import resolve_entry
    from unknown_agent.reasoning import supports_reasoning as reasoning_heuristic

    model_id = (model or config.model or "").strip()
    entry = resolve_entry(config.type)
    caps = ModelCapabilities(provider=provider_name, model=model_id)
    caps.context_window = config.context_window
    caps.max_output_tokens = config.max_output_tokens

    # tools -------------------------------------------------------------
    if config.supports_tools is False:
        caps.tools = Capability(False, "profile")
    elif model_info is not None and getattr(model_info, "supports_tools", None) is not None:
        caps.tools = Capability(bool(model_info.supports_tools), "model-metadata")
    elif entry is not None:
        caps.tools = Capability(entry.supports_tools, "catalog")
    else:
        caps.tools = Capability(True, "default")

    # vision --------------------------------------------------------------
    if config.supports_vision is not None:
        caps.vision = Capability(bool(config.supports_vision), "profile")
    elif model_info is not None and getattr(model_info, "supports_vision", False):
        caps.vision = Capability(True, "model-metadata")
    elif model_id and _heuristic_vision(model_id):
        # Model-id evidence beats the vendor-wide flag: an openrouter profile
        # "supports vision" as a vendor, but `deepseek-chat` does not.
        caps.vision = Capability(True, "heuristic")
    elif entry is not None and not entry.supports_vision:
        caps.vision = Capability(False, "catalog")
    else:
        caps.vision = Capability(False, "default")

    # reasoning -----------------------------------------------------------
    if config.supports_reasoning is not None:
        caps.reasoning = Capability(bool(config.supports_reasoning), "profile")
    elif model_info is not None and getattr(model_info, "supports_reasoning", False):
        caps.reasoning = Capability(True, "model-metadata")
    else:
        try:
            heuristic = bool(reasoning_heuristic(model_id, vendor=config.type))
        except TypeError:
            heuristic = bool(reasoning_heuristic(model_id))
        if heuristic:
            caps.reasoning = Capability(True, "heuristic")
        elif entry is not None:
            caps.reasoning = Capability(entry.supports_reasoning, "catalog")

    # streaming / json ------------------------------------------------------
    caps.streaming = Capability(bool(config.supports_streaming), "profile" if config.supports_streaming is not True else "catalog" if entry else "default")
    if entry is not None and not entry.supports_streaming:
        caps.streaming = Capability(False, "catalog")
    caps.json_mode = Capability(bool(config.json_mode), "profile")

    # computer use ----------------------------------------------------------
    # No profile override field yet (deliberate: the feature is gated by
    # [computer_use] enabled); the answer is model evidence or nothing.
    if model_id and _heuristic_computer_use(model_id):
        caps.computer_use = Capability(True, "heuristic")

    # limitations -------------------------------------------------------------
    if not caps.tools.value:
        caps.limitations.append("no native tool calling: the agent falls back to text-protocol tools")
    if not caps.vision.value:
        caps.limitations.append("not vision-capable: image tasks route to [vision] fallback_profile or are explained")
    if config.type == "offline":
        caps.limitations.append("offline provider: deterministic canned responses, no network")
    if entry is not None and entry.local:
        caps.limitations.append("local model: capabilities depend on the exact weights pulled")
    return caps


_MODEL_SPLIT = re.compile(r"[/:]")


def short_model_name(model_id: str) -> str:
    """`openrouter/anthropic/claude-3.5` -> `claude-3.5` (display helper)."""

    parts = [part for part in _MODEL_SPLIT.split(model_id) if part]
    return parts[-1] if parts else model_id
