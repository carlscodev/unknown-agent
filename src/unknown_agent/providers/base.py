"""Provider abstraction: the only place that knows about model APIs.

The agent core speaks in :class:`Message`, :class:`ToolCall` and
:class:`ModelResponse`; providers translate that to/from vendor wire formats.
Adding a vendor means adding one file and one registry entry - never touching the
loop, the planner, the memory or the CLI.

Contracts every provider must honour:

1. **Never raise for a model-level error** - return ``ModelResponse`` with
   ``error`` set for retryable/terminal problems after internal retries; raise
   :class:`ProviderAuthError` only when credentials are unusable.
2. **Report usage** - token counts feed budgets; a provider that cannot report
   real numbers must still report an estimate.
3. **Be cancellation-safe** - ``asyncio.CancelledError`` must propagate (the CLI
   uses it for Ctrl-C) and HTTP clients must be closed in ``aclose``.
"""

from __future__ import annotations

import abc
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from unknown_agent.core.models import Message, ToolCall

FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error", "cancelled"]


@dataclass(slots=True)
class ToolSchema:
    """Provider-neutral tool description."""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass(slots=True)
class ModelRequest:
    messages: list[Message]
    tools: Sequence[ToolSchema] = ()
    temperature: float = 0.2
    max_output_tokens: int = 8000
    json_mode: bool = False
    stop: Sequence[str] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Vendor-neutral reasoning parameters produced by :mod:`unknown_agent.reasoning`.
    #: Adapters translate them into their own dialect and drop what they cannot
    #: honour, so a caller never has to know which vendor is behind the request.
    reasoning: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.estimated = self.estimated or other.estimated


@dataclass(slots=True)
class ModelResponse:
    content: str = ""
    #: Internal reasoning text when the vendor exposes it (``reasoning_content``,
    #: ``thinking``).  Never mixed into ``content``.
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason = "stop"
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    error: str | None = None
    retries: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "reasoning": self.reasoning,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "finish_reason": self.finish_reason,
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
                "estimated": self.usage.estimated,
            },
            "model": self.model,
            "provider": self.provider,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "retries": self.retries,
        }


@dataclass(slots=True)
class StreamChunk:
    """Incremental model output.

    ``delta`` is visible text, ``reasoning`` is the model's *internal* thinking
    (OpenAI ``reasoning_content`` / Anthropic ``thinking``), and ``tool_call``
    is a complete call.  Keeping reasoning on its own field is what lets a UI
    show the thinking window exactly where the answer is being written instead
    of faking one above the composer.
    """

    delta: str = ""
    reasoning: str = ""
    tool_call: ToolCall | None = None
    finish_reason: FinishReason | None = None
    usage: Usage | None = None
    error: str | None = None


class ModelProvider(abc.ABC):
    """Base class for every provider implementation."""

    #: Registry key, e.g. ``openai-compatible``.
    name: str = "base"
    #: Set to False when the backend cannot do native tool calling.
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_json_mode: bool = False

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 8000,
        context_window: int = 128_000,
        request_timeout_s: float = 120.0,
        connect_timeout_s: float = 15.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
        vendor: str | None = None,
    ) -> None:
        #: Catalog key of the vendor this adapter was configured for (``groq``), which
        #: is what users see in messages - the adapter dialect is an implementation
        #: detail.
        self.vendor = vendor or type(self).__name__
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.context_window = context_window
        self.request_timeout_s = request_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.max_retries = max_retries
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})

    # -- tool-call contract ------------------------------------------------
    #: Every adapter implements these three methods so the core can translate tool
    #: schemas out and tool calls back in without knowing which vendor it is talking
    #: to.  Defaults speak the OpenAI dialect (the de-facto standard); Anthropic and
    #: Ollama override what differs.
    def encode_tools(self, schemas: list[ToolSchema]) -> list[dict[str, Any]]:
        from unknown_agent.providers.toolkit import tools_to_openai

        return tools_to_openai(schemas)

    def canonical_tool_response(self) -> dict[str, Any]:
        """A minimal, valid response of this dialect that contains one tool call.

        Used by :func:`unknown_agent.providers.toolkit.check_tool_roundtrip` to prove
        the decode path works before the agent depends on it.
        """

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_probe",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    def parse_tool_calls(self, payload: dict[str, Any]) -> list[ToolCall]:
        from unknown_agent.providers.toolkit import tool_call_from_openai

        return [
            tool_call_from_openai(slot)
            for slot in (payload.get("choices", [{}])[0].get("message", {}).get("tool_calls") or [])
        ]

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:  # pragma: no cover - overridden by HTTP providers
        """Release network resources.  Safe to call multiple times."""
        return None

    async def __aenter__(self) -> ModelProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- contract ----------------------------------------------------------
    @abc.abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one assistant turn."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        """Yield incremental chunks.  Default: single non-streamed chunk."""

        response = await self.complete(request)
        if response.content:
            yield StreamChunk(delta=response.content)
        for call in response.tool_calls:
            yield StreamChunk(tool_call=call)
        yield StreamChunk(finish_reason=response.finish_reason, usage=response.usage, error=response.error)

    # -- helpers -----------------------------------------------------------
    def estimate_tokens(self, text: str) -> int:
        """Cheap, provider-independent token estimate (~4 chars/token).

        Used only for budgeting and context packing; real counts come from usage
        reports when the provider supplies them.
        """

        return max(1, len(text) // 4)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "supports_json_mode": self.supports_json_mode,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
        }

    @staticmethod
    def timed() -> float:
        return time.perf_counter()
