"""Anthropic Messages API provider.

Translates the neutral message model into Anthropic's ``system`` + ``messages``
shape, including tool_use / tool_result content blocks and SSE streaming with
``input_json_delta`` reassembly.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from unknown_agent.core.models import Message, ToolCall
from unknown_agent.errors import ProviderAuthError
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import (
    FinishReason,
    ToolSchema,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamChunk,
    Usage,
)
from unknown_agent.providers.openai_compatible import RETRYABLE_STATUS, _backoff

log = get_logger("providers.anthropic")

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    supports_json_mode = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = (self.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": API_VERSION,
            }
            if self.api_key:
                headers["x-api-key"] = self.api_key
            headers.update(self.headers)
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.request_timeout_s, connect=self.connect_timeout_s),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- payload -----------------------------------------------------------
    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            if message.role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": message.tool_call_id or "unknown", "content": message.content or ""}
                        ],
                    }
                )
                continue
            if message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
                messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
                continue
            user_blocks: list[dict[str, Any]] = [{"type": "text", "text": message.content or ""}]
            for image in getattr(message, "images", None) or []:
                block = _image_block(image)
                if block is not None:
                    user_blocks.append(block)
            messages.append({"role": "user", "content": user_blocks})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens or self.max_output_tokens,
            "temperature": request.temperature if request.temperature is not None else self.temperature,
            "messages": messages,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.tools and self.supports_tools:
            payload["tools"] = [
                {"name": tool.name, "description": tool.description, "input_schema": tool.parameters} for tool in request.tools
            ]
        if request.reasoning:
            from unknown_agent.reasoning import anthropic_reasoning_params

            payload = anthropic_reasoning_params(payload, request.reasoning)
        return payload

    # -- tool-call contract (Anthropic dialect) ----------------------------
    def encode_tools(self, schemas: list[ToolSchema]) -> list[dict[str, Any]]:
        from unknown_agent.providers.toolkit import tools_to_anthropic

        return tools_to_anthropic(schemas)

    def canonical_tool_response(self) -> dict[str, Any]:
        return {
            "id": "msg_probe",
            "type": "message",
            "role": "assistant",
            "model": "probe",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "I will read the file."},
                {"type": "tool_use", "id": "toolu_probe", "name": "read_file", "input": {"path": "README.md"}},
            ],
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }

    def parse_tool_calls(self, payload: dict[str, Any]) -> list[ToolCall]:
        from unknown_agent.providers.toolkit import tool_call_from_anthropic

        return [
            tool_call_from_anthropic(block)
            for block in payload.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]


    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self._payload(request, stream=False)
        started = self.timed()
        attempt = 0
        last_error: str | None = None
        while attempt <= self.max_retries:
            attempt += 1
            try:
                response = await self.client.post("/messages", json=payload)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code in {401, 403}:
                    raise ProviderAuthError(
                        f"Anthropic rejected the credentials (HTTP {response.status_code}).",
                        hint="Set anthropic.api_key to env:ANTHROPIC_API_KEY and run `ua providers test`.",
                    )
                if response.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    await _sleep(_backoff(attempt, response.headers.get("retry-after")))
                    continue
                if response.status_code >= 400:
                    return ModelResponse(error=f"HTTP {response.status_code}: {response.text[:500]}", finish_reason="error", provider=self.name, model=self.model)
                return self._parse(response.json(), started, retries=attempt - 1)
        return ModelResponse(error=last_error or "request failed", finish_reason="error", provider=self.name, model=self.model)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        payload = self._payload(request, stream=True)
        buffer: dict[str, Any] = {}
        try:
            async with self.client.stream("POST", "/messages", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    yield StreamChunk(error=f"HTTP {response.status_code}: {body}", finish_reason="error")
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            buffer = {"id": block.get("id", ""), "name": block.get("name", ""), "json": ""}
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield StreamChunk(delta=delta["text"])
                        elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                            # Extended thinking: internal reasoning, kept out of
                            # the visible answer so the UI can show it separately.
                            yield StreamChunk(reasoning=delta["thinking"])
                        elif delta.get("type") == "input_json_delta":
                            buffer["json"] = buffer.get("json", "") + (delta.get("partial_json") or "")
                    elif kind == "content_block_stop" and buffer.get("name"):
                        arguments = _safe_json(buffer.get("json", ""))
                        yield StreamChunk(tool_call=ToolCall(id=buffer.get("id") or ToolCall.new(buffer["name"]).id, name=buffer["name"], arguments=arguments, raw_arguments=buffer.get("json")))
                        buffer = {}
                    elif kind == "message_delta":
                        usage = (event.get("usage") or {})
                        if usage:
                            yield StreamChunk(
                                usage=Usage(
                                    completion_tokens=int(usage.get("output_tokens", 0)),
                                    prompt_tokens=int((event.get("message") or {}).get("usage", {}).get("input_tokens", 0)),
                                )
                            )
                        stop_reason = (event.get("delta") or {}).get("stop_reason")
                        if stop_reason:
                            yield StreamChunk(finish_reason=_map_stop(stop_reason))
                    elif kind == "error":
                        yield StreamChunk(error=str(event.get("error")), finish_reason="error")
        except httpx.HTTPError as exc:
            yield StreamChunk(error=f"transport error: {exc}", finish_reason="error")

    def _parse(self, body: dict[str, Any], started: float, *, retries: int) -> ModelResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in body.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block.get("id") or ToolCall.new(block.get("name", "tool")).id, name=block.get("name", ""), arguments=block.get("input") or {}))
        usage = body.get("usage") or {}
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=calls,
            finish_reason=_map_stop(body.get("stop_reason")),
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
                total_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
            ),
            model=body.get("model", self.model),
            provider=self.name,
            latency_ms=(self.timed() - started) * 1000,
            retries=retries,
        )


def _image_block(image_url: str) -> dict[str, Any] | None:
    """Anthropic image block from a data URL or an https URL.

    ``Message.images`` carries provider-neutral URLs; the OpenAI dialect sends
    them as ``image_url`` parts, Anthropic wants explicit ``source`` objects
    (base64 for data URLs, ``url`` for remote).  Anything unparseable is
    dropped rather than breaking the request - the text half still stands.
    """

    if image_url.startswith("data:"):
        header, _, data = image_url.partition(",")
        media_type = header[5:].partition(";")[0] or "image/png"
        if not data:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    if image_url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": image_url}}
    return None


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _safe_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"_raw": raw}


def _map_stop(reason: str | None) -> FinishReason:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }.get(reason or "end_turn", "stop")


def build(**kwargs: Any) -> ModelProvider:
    return AnthropicProvider(**kwargs)


__all__ = ["AnthropicProvider", "build"]
