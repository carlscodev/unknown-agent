"""OpenAI-compatible provider: OpenAI, Azure OpenAI, Groq, Together, vLLM,
LM Studio, OpenRouter, Fireworks, DeepSeek, xAI and anything else that speaks
``/chat/completions``.

This is the default provider because it maximises reach with one implementation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from unknown_agent.core.models import Message, ToolCall
from unknown_agent.errors import ProviderAuthError, ProviderError
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import (
    FinishReason,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamChunk,
    Usage,
)

log = get_logger("providers.openai")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}


class OpenAICompatibleProvider(ModelProvider):
    name = "openai-compatible"
    supports_json_mode = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.headers)
        self._client: httpx.AsyncClient | None = None

    # -- HTTP plumbing -----------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers_with_auth(self.headers, self.api_key),
                timeout=httpx.Timeout(self.request_timeout_s, connect=self.connect_timeout_s),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- payloads ----------------------------------------------------------
    #: Output budget we fall back to when the account cannot afford the configured one.
    SHORT_OUTPUT_MAX_TOKENS = 1024

    def _shrink_payload(self, payload: dict[str, Any], body: str) -> dict[str, Any] | None:
        """Adapt to "not enough credits / max_tokens too large" responses.

        Providers say exactly what to do in this case (OpenRouter: *"fewer
        max_tokens"*).  Failing the whole task would be the wrong product behaviour,
        so the adapter retries once with a smaller output budget - 1k tokens is
        plenty for a tool call and a short step summary.
        """

        lowered = body.lower()
        if not any(marker in lowered for marker in ("credit", "insufficient", "max_tokens", "max tokens")):
            return None
        current = int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 0)
        if not current or current <= self.SHORT_OUTPUT_MAX_TOKENS:
            return None
        trimmed = dict(payload)
        trimmed["max_tokens"] = self.SHORT_OUTPUT_MAX_TOKENS
        trimmed.pop("max_completion_tokens", None)
        log.info("provider asked for a smaller output budget; retrying with max_tokens=%s", self.SHORT_OUTPUT_MAX_TOKENS)
        return trimmed

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._encode_message(message) for message in request.messages],
            "temperature": request.temperature if request.temperature is not None else self.temperature,
            "max_tokens": request.max_output_tokens or self.max_output_tokens,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.tools and self.supports_tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}
                for tool in request.tools
            ]
            payload["tool_choice"] = "auto"
        if request.json_mode and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.extra.get("parallel_tool_calls") is not None:
            payload["parallel_tool_calls"] = self.extra["parallel_tool_calls"]
        if request.reasoning:
            from unknown_agent.reasoning import openai_reasoning_params

            payload = openai_reasoning_params(payload, request.reasoning, vendor=self.vendor)
        return payload

    @staticmethod
    def _encode_message(message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role}
        content: Any = message.content
        if message.images:
            content = [{"type": "text", "text": message.content or ""}]
            content.extend({"type": "image_url", "image_url": {"url": url}} for url in message.images)
        payload["content"] = content or ""
        if message.tool_calls:
            payload["tool_calls"] = [
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.name:
            payload["name"] = message.name
        return payload

    # -- requests ----------------------------------------------------------
    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self._payload(request, stream=False)
        started = self.timed()
        attempt = 0
        last_error: str | None = None
        while attempt <= self.max_retries:
            attempt += 1
            try:
                response = await self.client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                last_error = f"timeout after {self.request_timeout_s:.0f}s: {exc}"
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code == 401 or response.status_code == 403:
                    raise ProviderAuthError(
                        f"{self.name} rejected the credentials (HTTP {response.status_code}).",
                        hint="Check the API key reference in your config and run `ua providers test`.",
                        context={"status": response.status_code, "body": response.text[:500]},
                    )
                if response.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                    last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    await asyncio.sleep(_backoff(attempt, response.headers.get("retry-after")))
                    continue
                if response.status_code >= 400:
                    body = response.text[:500]
                    trimmed = self._shrink_payload(payload, body)
                    if trimmed is not None and attempt <= self.max_retries:
                        payload = trimmed
                        continue
                    return ModelResponse(
                        error=f"HTTP {response.status_code}: {body}",
                        finish_reason="error",
                        model=self.model,
                        provider=self.name,
                        latency_ms=(self.timed() - started) * 1000,
                        retries=attempt - 1,
                    )
                return self._parse(response.json(), started, retries=attempt - 1)
        return ModelResponse(
            error=last_error or "request failed",
            finish_reason="error",
            model=self.model,
            provider=self.name,
            latency_ms=(self.timed() - started) * 1000,
            retries=attempt - 1,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        payload = self._payload(request, stream=True)
        started = self.timed()
        try:
            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    yield StreamChunk(error=f"HTTP {response.status_code}: {body}", finish_reason="error")
                    return
                pending: dict[int, dict[str, Any]] = {}
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        yield StreamChunk(
                            usage=Usage(
                                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                                completion_tokens=int(usage.get("completion_tokens", 0)),
                                total_tokens=int(usage.get("total_tokens", 0)),
                            )
                        )
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            yield StreamChunk(delta=delta["content"])
                        for tool_delta in delta.get("tool_calls") or []:
                            index = int(tool_delta.get("index", 0))
                            slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if tool_delta.get("id"):
                                slot["id"] = tool_delta["id"]
                            function = tool_delta.get("function") or {}
                            if function.get("name"):
                                slot["name"] += function["name"]
                            if function.get("arguments"):
                                slot["arguments"] += function["arguments"]
                        finish = choice.get("finish_reason")
                        if finish:
                            for slot in pending.values():
                                if slot["name"]:
                                    yield StreamChunk(tool_call=_to_tool_call(slot))
                            pending.clear()
                            yield StreamChunk(finish_reason=_map_finish(finish))
        except httpx.TimeoutException:
            yield StreamChunk(error=f"stream timeout after {self.request_timeout_s:.0f}s", finish_reason="error")
        except httpx.HTTPError as exc:
            yield StreamChunk(error=f"transport error: {exc}", finish_reason="error")
        finally:
            log.debug("stream finished in %.2fs", self.timed() - started)

    # -- parsing -----------------------------------------------------------
    def _parse(self, body: dict[str, Any], started: float, *, retries: int) -> ModelResponse:
        choices = body.get("choices") or []
        if not choices:
            return ModelResponse(
                error="provider returned no choices",
                finish_reason="error",
                model=body.get("model", self.model),
                provider=self.name,
                latency_ms=(self.timed() - started) * 1000,
                retries=retries,
            )
        choice = choices[0]
        message = choice.get("message") or {}
        calls = [
            ToolCall(
                id=item.get("id") or ToolCall.new(item.get("function", {}).get("name", "tool")).id,
                name=(item.get("function") or {}).get("name", ""),
                arguments=_loads((item.get("function") or {}).get("arguments")),
                raw_arguments=(item.get("function") or {}).get("arguments"),
            )
            for item in (message.get("tool_calls") or [])
        ]
        raw_usage = body.get("usage") or {}
        return ModelResponse(
            content=message.get("content") or "",
            tool_calls=[call for call in calls if call.name],
            finish_reason=_map_finish(choice.get("finish_reason")),
            usage=Usage(
                prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
                completion_tokens=int(raw_usage.get("completion_tokens", 0)),
                total_tokens=int(raw_usage.get("total_tokens", 0)),
            ),
            model=body.get("model", self.model),
            provider=self.name,
            latency_ms=(self.timed() - started) * 1000,
            retries=retries,
            raw={"id": body.get("id"), "finish_reason": choice.get("finish_reason")},
        )


def headers_with_auth(headers: dict[str, str], api_key: str | None) -> dict[str, str]:
    merged = dict(headers)
    if api_key and "Authorization" not in merged and "api-key" not in merged:
        merged["Authorization"] = f"Bearer {api_key}"
    return merged


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"_raw": str(raw)}


def _to_tool_call(slot: dict[str, Any]) -> ToolCall:
    return ToolCall(id=slot.get("id") or ToolCall.new(slot.get("name", "tool")).id, name=slot["name"], arguments=_loads(slot.get("arguments")), raw_arguments=slot.get("arguments"))


def _map_finish(reason: str | None) -> FinishReason:
    return {
        "stop": "stop",
        "tool_calls": "tool_calls",
        "length": "length",
        "content_filter": "content_filter",
        "function_call": "tool_calls",
    }.get(reason or "stop", "stop")


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(30.0, max(0.5, float(retry_after)))
        except ValueError:
            pass
    return min(20.0, 0.8 * (2 ** (attempt - 1)))


def build(**kwargs: Any) -> ModelProvider:
    return OpenAICompatibleProvider(**kwargs)


__all__ = ["OpenAICompatibleProvider", "build"]
