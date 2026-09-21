"""Tool-call normalisation: one internal contract, many vendor dialects.

The agent core only ever sees :class:`~unknown_agent.core.models.ToolCall` and
:class:`~unknown_agent.providers.base.ToolSchema`.  This module owns the two jobs
that keep that promise honest:

* **translation** - internal schemas/messages to each vendor's wire format and back
  (OpenAI ``tools``/``tool_calls``, Anthropic ``tool_use``/``tool_result`` blocks,
  Ollama's OpenAI-compatible shape);
* **verification** - a round-trip self-check (:func:`check_tool_roundtrip`) that
  proves a provider can carry a tool call end to end *before* the agent depends on
  it.  Onboarding and ``ua doctor`` call it, so a broken integration is caught at
  setup time instead of in the middle of a task.

Errors are normalised too (:func:`normalise_provider_error`), because "insufficient
credits", "model not found" and "context length exceeded" need different fixes and
must never be reported as a generic failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from unknown_agent.core.models import Message, ToolCall
from unknown_agent.errors import ProviderError
from unknown_agent.providers import resilience
from unknown_agent.providers.base import ModelProvider, ToolSchema
from unknown_agent.providers.resilience import FailureKind

# --------------------------------------------------------------------------- #
# translation
# --------------------------------------------------------------------------- #
def tools_to_openai(schemas: list[ToolSchema]) -> list[dict[str, Any]]:
    """Internal tool schemas -> OpenAI ``tools`` array (used by most vendors)."""

    return [
        {
            "type": "function",
            "function": {
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters or {"type": "object", "properties": {}},
            },
        }
        for schema in schemas
    ]


def tools_to_anthropic(schemas: list[ToolSchema]) -> list[dict[str, Any]]:
    """Internal tool schemas -> Anthropic ``tools`` array."""

    return [
        {
            "name": schema.name,
            "description": schema.description,
            "input_schema": schema.parameters or {"type": "object", "properties": {}},
        }
        for schema in schemas
    ]


def tool_call_from_openai(slot: dict[str, Any]) -> ToolCall:
    function = slot.get("function") or {}
    call = ToolCall.new(str(function.get("name") or ""), _loads(function.get("arguments")))
    call.id = str(slot.get("id") or call.id)
    return call


def tool_call_from_anthropic(block: dict[str, Any]) -> ToolCall:
    payload = block.get("input")
    arguments = payload if isinstance(payload, dict) else _loads(payload)
    call = ToolCall.new(str(block.get("name") or ""), arguments)
    call.id = str(block.get("id") or call.id)
    return call


def tool_calls_to_anthropic_blocks(calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in calls]


def tool_result_blocks(message: Message) -> list[dict[str, Any]]:
    """An internal tool message -> Anthropic ``tool_result`` content blocks."""

    return [
        {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id or message.name or "tool",
            "content": message.content,
            "is_error": bool(getattr(message, "error", False)),
        }
    ]


# --------------------------------------------------------------------------- #
# capability + health checks
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RoundtripReport:
    ok: bool
    checks: list[tuple[str, bool, str]]

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [item for item in self.checks if not item[1]]

    def render(self) -> str:
        return "\n".join(f"{'ok ' if passed else 'FAIL'} {name}: {detail}" for name, passed, detail in self.checks)


def check_tool_roundtrip(provider: ModelProvider, *, schemas: list[ToolSchema] | None = None) -> RoundtripReport:
    """Prove that this provider can carry tool calls, without touching the network.

    The check exercises the *adapter*: schema serialisation for its dialect, plus
    parsing of a canonical response back into an internal :class:`ToolCall`.  It is
    deliberately provider-agnostic - each adapter implements ``encode_tools`` /
    ``parse_tool_calls`` and this function verifies the contract they share.
    """

    schema = ToolSchema(
        name="read_file",
        description="Read a file.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File to read."}},
            "required": ["path"],
        },
    )
    sample = [schema] if schemas is None else schemas
    checks: list[tuple[str, bool, str]] = []

    declared = bool(getattr(provider, "supports_tools", False))
    checks.append(("capability", declared or provider.name == "offline", f"supports_tools={declared}"))

    encoded = provider.encode_tools(sample) if hasattr(provider, "encode_tools") else None
    encoded_ok = bool(encoded) and any(schema.name in json.dumps(item) for item in encoded)
    checks.append(("encode", encoded_ok, f"{len(encoded or [])} tool(s) encoded for {provider.name}"))

    canonical = provider.canonical_tool_response()
    parsed = provider.parse_tool_calls(canonical) if hasattr(provider, "parse_tool_calls") else []
    parsed_ok = bool(parsed) and parsed[0].name == "read_file" and parsed[0].arguments.get("path")
    checks.append(("decode", parsed_ok, f"parsed {parsed[0].name if parsed else 'nothing'} back into a ToolCall"))

    return RoundtripReport(ok=all(item[1] for item in checks), checks=checks)


def capability_report(profile: Any, entry: Any = None) -> dict[str, Any]:
    """Everything the product needs to know before promising a feature."""

    return {
        "provider": getattr(profile, "type", "unknown"),
        "model": getattr(profile, "model", ""),
        "tools": bool(getattr(profile, "supports_tools", True)),
        "streaming": bool(getattr(profile, "supports_streaming", True)),
        "json_mode": bool(getattr(profile, "json_mode", True)),
        "model_listing": bool(getattr(entry, "model_listing", "none") != "none") if entry else None,
        "reasoning": bool(getattr(entry, "supports_reasoning", False)) if entry else None,
        "vision": bool(getattr(entry, "supports_vision", False)) if entry else None,
        "context_window": getattr(profile, "context_window", None),
    }


def normalise_provider_error(error: Exception, *, provider: str, model: str = "") -> ProviderError:
    """Map any provider failure onto one taxonomy with actionable hints.

    Delegates to :mod:`unknown_agent.providers.resilience` so the CLI, the agent
    loop and the TUI describe the same failure identically.

    This used to be a private copy of the classifier, and the copy is what broke:
    it read ``error.status_code`` — which the HTTP layer never sets, because the
    status was already flattened into the message as ``"HTTP 429: ..."`` — and it
    matched the literal substring ``"rate limit"``, which unli.dev does not send.
    Its capacity error therefore fell all the way through to
    ``"{provider} failed: <raw JSON>"`` with the hint "Re-run with --debug",
    and the user saw a Chinese error body with no way to act on it.

    The tool-capability branch stays here: it is about *our* request shape rather
    than a provider state, and the shared taxonomy has no kind for it.
    """

    if isinstance(error, ProviderError):
        return error

    failure = resilience.classify(error, provider=provider, model=model)

    # Tool-shape mismatch: "you asked for tools this model cannot take".
    lowered = str(error).lower()
    if failure.kind == FailureKind.UNKNOWN and "tool" in lowered and (
        "support" in lowered or "unknown" in lowered or "not allowed" in lowered
    ):
        return ProviderError(
            f"{provider} cannot call tools the way this task needs ({failure.summary()}).",
            hint="Pick a tool-capable model (`ua models list --tools`) or an OpenAI-compatible provider.",
        )

    hint = _hint_for_kind(failure, provider=provider)
    return ProviderError(
        failure.summary(),
        hint=hint,
        context={
            "kind": failure.kind,
            "status": failure.status_code,
            "retryable": failure.retryable,
            "retry_after": failure.retry_after,
            "provider_code": failure.provider_code,
        },
    )


def _hint_for_kind(failure: resilience.Failure, *, provider: str) -> str:
    """Prefer the shared hint; keep the product-specific commands for the CLI."""

    product_hints = {
        FailureKind.INVALID_CREDENTIALS: "Run `ua setup` to re-enter the key, or check the environment variable it references.",
        FailureKind.CREDITS_EXHAUSTED: "Top up the account, or switch model/provider with `ua models use` / `ua providers use`.",
        FailureKind.NOT_FOUND: "Run `ua models list` to see what this key can actually call.",
        FailureKind.CONTEXT_LENGTH: "Lower agent.context_token_budget, or switch to a model with a larger window.",
        FailureKind.TIMEOUT: "Increase providers.<name>.request_timeout_s or retry; local models may simply be slow.",
        FailureKind.INCOMPATIBLE_API: "Re-run `ua setup` and pick compatibility `anthropic` or `auto`.",
    }
    if failure.kind in product_hints:
        return product_hints[failure.kind]
    if failure.kind == FailureKind.NO_CAPACITY:
        wait = f" Waiting about {failure.retry_after:.0f}s before the next attempt." if failure.retry_after else ""
        return (
            f"{provider}'s upstream is out of capacity — your credentials are fine.{wait} "
            "Automatic retry is bounded and backed off; switch provider with `ua providers use` if it persists."
        )
    if failure.kind == FailureKind.RATE_LIMITED:
        wait = f" Retry in about {failure.retry_after:.0f}s." if failure.retry_after else ""
        return f"Backoff is automatic and bounded; `agent.max_retries` controls it.{wait}"
    return failure.hint or "Re-run with --debug for the full response."


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"_raw": str(raw)[:400]}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


__all__ = [
    "RoundtripReport",
    "capability_report",
    "check_tool_roundtrip",
    "normalise_provider_error",
    "tool_call_from_anthropic",
    "tool_call_from_openai",
    "tool_calls_to_anthropic_blocks",
    "tool_result_blocks",
    "tools_to_anthropic",
    "tools_to_openai",
]
