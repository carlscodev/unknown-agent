"""Provider API compatibility handling.

The catalog used to assume every provider's compatibility was known from its
entry (``protocol`` == ``openai`` or ``anthropic``).  That left custom
endpoints – an UNLI gateway, a company LiteLLM proxy, a vLLM instance – in a
hole: the only way to configure them was to pick a catalog vendor and override
its base URL, which is both dishonest and brittle.

This module makes compatibility *explicit* and selectable:

* ``openai``        – OpenAI Chat Completions (``POST /chat/completions``) and
  ``GET /models`` with ``Authorization: Bearer``.  The de-facto standard.
* ``anthropic``     – Anthropic Messages (``POST /messages``) and ``GET /models``
  with ``x-api-key``.
* ``auto``          – try OpenAI first, then Anthropic, report which succeeded.

Every future provider format gets a case here rather than spread across the TUI.
"""

from __future__ import annotations

from typing import Literal

Compatibility = Literal["openai", "anthropic", "auto"]

# Human labels shown in the TUI selector.
COMPATIBILITY_LABELS: dict[str, str] = {
    "openai": "OpenAI-compatible",
    "anthropic": "Anthropic-compatible",
    "auto": "Auto-detect (try OpenAI, then Anthropic)",
}

# Longer explanations rendered in the detail pane.
COMPATIBILITY_DESCRIPTIONS: dict[str, str] = {
    "openai": "Speaks OpenAI chat/completions (Bearer token, /v1/models, /chat/completions). Used by UNLI, OpenAI, Groq, vLLM, LiteLLM and most gateways.",
    "anthropic": "Speaks Anthropic Messages (x-api-key, /v1/models, /messages). Used by Anthropic and proxies that emulate it.",
    "auto": "Probe the endpoint: try OpenAI semantics first, fall back to Anthropic. Useful when docs are unclear; adds one extra request.",
}

ORDER: tuple[str, ...] = ("openai", "anthropic", "auto")


def normalize_compatibility(value: str | None) -> Compatibility:
    """Coerce a stored or user-supplied value to a canonical compatibility.

    Accepts the config type aliases (``openai-compatible``, ``openai_compatible``)
    as well as the short forms.
    """

    if not value:
        return "openai"
    lower = value.strip().lower().replace("-", "_").replace(" ", "_")
    if lower in {"openai", "openai_compatible", "openai_compat"}:
        return "openai"
    if lower in {"anthropic", "anthropic_compatible"}:
        return "anthropic"
    if lower in {"auto", "auto_detect", "unknown", "detect"}:
        return "auto"
    return "openai"


def protocol_for_compatibility(comp: str) -> str:
    """Map a compatibility to the adapter protocol needed to talk to it."""

    comp = normalize_compatibility(comp)
    if comp == "anthropic":
        return "anthropic"
    return "openai"


def listing_for_compatibility(comp: str) -> str:
    """Model listing flavour that corresponds to a compatibility."""

    comp = normalize_compatibility(comp)
    if comp == "anthropic":
        return "anthropic"
    return "openai"


def label_for(comp: str) -> str:
    return COMPATIBILITY_LABELS.get(normalize_compatibility(comp), comp)


def description_for(comp: str) -> str:
    return COMPATIBILITY_DESCRIPTIONS.get(normalize_compatibility(comp), "")


__all__ = [
    "Compatibility",
    "COMPATIBILITY_LABELS",
    "COMPATIBILITY_DESCRIPTIONS",
    "ORDER",
    "normalize_compatibility",
    "protocol_for_compatibility",
    "listing_for_compatibility",
    "label_for",
    "description_for",
]
