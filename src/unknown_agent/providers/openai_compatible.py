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
from unknown_agent.providers import resilience
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


def _policy_for(provider: ModelProvider) -> resilience.RetryPolicy:
    """Build the retry policy for an adapter from its own ``max_retries``.

    The adapter stays the source of truth for *how many* attempts (so
    ``providers.<name>.max_retries`` in config still means something), while the
    shape — capped exponential with jitter, ``Retry-After`` honoured and clamped —
    lives in one place.
    """

    return resilience.RetryPolicy(
        max_retries=max(0, int(provider.max_retries)),
        base_delay=0.8,
        max_delay=20.0,
        jitter=0.25,
        max_retry_after=60.0,
    )


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
        #: Some gateways (unli among them) route a turn that contains a
        #: ``role: "tool"`` message to a *different* upstream that may be down
        #: (HTTP 502 ``upstream_error``) while the normal path answers fine.
        #: When that happens we re-encode tool results as plain user turns once
        #: and remember it, so a multi-step agent run keeps working instead of
        #: dying on the second step.
        self.tool_results_as_user = bool((self.extra or {}).get("tool_results_as_user", False))

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
            "messages": [
                self._encode_message(message, tool_results_as_user=self.tool_results_as_user)
                for message in request.messages
            ],
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
    def _has_tool_results(request: ModelRequest) -> bool:
        return any(message.role == "tool" for message in request.messages)

    def _switch_tool_result_encoding(self, request: ModelRequest, status_code: int | None) -> bool:
        """Re-encode tool results as user turns after a gateway upstream failure.

        Returns True when the caller should retry the same request.
        """

        if self.tool_results_as_user or not self._has_tool_results(request):
            return False
        if status_code is not None and status_code not in (500, 502, 503, 504):
            return False
        self.tool_results_as_user = True
        log.warning(
            "%s rejected a tool-result turn (HTTP %s); re-encoding tool results as user turns",
            self.vendor,
            status_code,
        )
        return True

    @staticmethod
    def _encode_message(message: Message, *, tool_results_as_user: bool = False) -> dict[str, Any]:
        if tool_results_as_user and message.role == "tool":
            # A tool observation delivered as an ordinary user turn. The model
            # still sees the result verbatim; only the wire role changes.
            label = message.name or message.tool_call_id or "tool"
            body = message.content or "(no output)"
            return {"role": "user", "content": f"[tool result: {label}]\n{body}"}
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
    #: Above this many seconds of remaining cooldown we fail fast with a message
    #: that says how long to wait, instead of silently sleeping in a request.
    #: The UI can then offer "retry now / switch provider" rather than looking dead.
    COOLDOWN_FAIL_FAST_S = 8.0

    @property
    def cooldown_key(self) -> str:
        return f"{self.vendor}:{self.model}"

    def _cooldown_block(self, started: float) -> ModelResponse | None:
        """Refuse to hammer a provider that already told us it has no capacity."""

        remaining = resilience.cooldown.remaining(self.cooldown_key)
        if remaining <= self.COOLDOWN_FAIL_FAST_S:
            return None
        reason = resilience.cooldown.reason(self.cooldown_key) or "the provider asked us to wait"
        return ModelResponse(
            error=(
                f"{self.vendor} is cooling down after a capacity failure; retry in about "
                f"{remaining:.0f}s. ({reason})"
            ),
            finish_reason="error",
            model=self.model,
            provider=self.name,
            latency_ms=(self.timed() - started) * 1000,
            retries=0,
            raw={"kind": resilience.cooldown.kind(self.cooldown_key), "cooldown_s": round(remaining, 1)},
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self._payload(request, stream=False)
        started = self.timed()
        policy = _policy_for(self)
        attempt = 0
        last_error: str | None = None
        last_failure: resilience.Failure | None = None

        blocked = self._cooldown_block(started)
        if blocked is not None:
            log.info("skipping request: %s", blocked.error)
            return blocked

        while attempt <= self.max_retries:
            attempt += 1
            try:
                response = await self.client.post("/chat/completions", json=payload)
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException as exc:
                last_error = f"timeout after {self.request_timeout_s:.0f}s: {exc}"
                last_failure = resilience.classify(exc, provider=self.vendor, model=self.model)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                last_failure = resilience.classify(exc, provider=self.vendor, model=self.model)
            else:
                if response.status_code == 401 or response.status_code == 403:
                    resilience.cooldown.clear(self.cooldown_key)
                    raise ProviderAuthError(
                        f"{self.name} rejected the credentials (HTTP {response.status_code}).",
                        hint="Check the API key reference in your config and run `ua providers test`.",
                        context={"status": response.status_code, "body": response.text[:500]},
                    )
                if response.status_code >= 400:
                    failure = resilience.classify(
                        f"HTTP {response.status_code}: {response.text[:400]}",
                        status_code=response.status_code,
                        headers=response.headers,
                        provider=self.vendor,
                        model=self.model,
                    )
                    last_failure = failure
                    last_error = failure.summary()

                    # A gateway whose *tool* upstream is down answers 5xx on any
                    # multi-step turn. Re-encode once instead of failing the run.
                    if self._switch_tool_result_encoding(request, response.status_code):
                        payload = self._payload(request, stream=False)
                        continue

                    # Retryable (429 / 5xx / capacity): bounded, jittered, and
                    # honouring Retry-After.  Never an immediate re-send.
                    if failure.retryable and attempt <= self.max_retries:
                        delay = policy.delay(attempt, failure)
                        resilience.cooldown.note(self.cooldown_key, failure)
                        log.warning(
                            "%s returned %s (%s); backing off %.1fs (attempt %d/%d)",
                            self.vendor,
                            failure.status_code,
                            failure.kind,
                            delay,
                            attempt,
                            policy.attempts(),
                        )
                        await asyncio.sleep(delay)
                        continue

                    # The account cannot afford this output budget: shrink and retry.
                    trimmed = self._shrink_payload(payload, response.text[:500])
                    if trimmed is not None and attempt <= self.max_retries:
                        payload = trimmed
                        continue

                    if failure.retryable:
                        # Out of attempts on a transient failure: remember it so the
                        # next message does not immediately re-hammer the provider.
                        resilience.cooldown.note(self.cooldown_key, failure)
                    else:
                        # A terminal failure means the provider is answering fine;
                        # any stale cooldown would only mislead the next request.
                        resilience.cooldown.clear(self.cooldown_key)
                    return self._error_response(failure, started, retries=attempt - 1)
                resilience.cooldown.clear(self.cooldown_key)
                return self._parse(response.json(), started, retries=attempt - 1)

            # Transport-level failure (timeout / connection): also bounded.
            if last_failure is not None and last_failure.retryable and attempt <= self.max_retries:
                delay = policy.delay(attempt, last_failure)
                log.warning("%s transport failure (%s); backing off %.1fs", self.vendor, last_failure.kind, delay)
                await asyncio.sleep(delay)
                continue
            break

        if last_failure is not None and last_failure.retryable:
            resilience.cooldown.note(self.cooldown_key, last_failure)
        return self._error_response(last_failure, started, retries=attempt - 1, fallback=last_error)

    def _error_response(
        self,
        failure: resilience.Failure | None,
        started: float,
        *,
        retries: int,
        fallback: str | None = None,
    ) -> ModelResponse:
        """An error the user can act on, with the raw body kept as evidence only."""

        if failure is None:
            return ModelResponse(
                error=fallback or "request failed",
                finish_reason="error",
                model=self.model,
                provider=self.name,
                latency_ms=(self.timed() - started) * 1000,
                retries=retries,
            )
        wait = f" Retry in about {failure.retry_after:.0f}s." if failure.retry_after else ""
        return ModelResponse(
            error=f"{failure.summary()}{wait}",
            finish_reason="error",
            model=self.model,
            provider=self.name,
            latency_ms=(self.timed() - started) * 1000,
            retries=retries,
            raw={
                "kind": failure.kind,
                "status": failure.status_code,
                "retryable": failure.retryable,
                "provider_code": failure.provider_code,
                "hint": failure.hint,
            },
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        payload = self._payload(request, stream=True)
        started = self.timed()
        policy = _policy_for(self)

        blocked = self._cooldown_block(started)
        if blocked is not None:
            yield StreamChunk(error=blocked.error, finish_reason="error")
            return

        # Retries cover *reaching* the stream only.  Once bytes have been yielded we
        # must never replay them, so a mid-stream failure surfaces as an error chunk
        # rather than a duplicate answer.
        attempt = 0
        try:
            while True:
                attempt += 1
                async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                        failure = resilience.classify(
                            f"HTTP {response.status_code}: {body}",
                            status_code=response.status_code,
                            headers=response.headers,
                            provider=self.vendor,
                            model=self.model,
                        )
                        # Nothing has been yielded yet, so a re-encoded retry
                        # is safe here (never once bytes are on the wire).
                        if self._switch_tool_result_encoding(request, response.status_code):
                            payload = self._payload(request, stream=True)
                            continue

                        if failure.retryable and attempt <= self.max_retries:
                            delay = policy.delay(attempt, failure)
                            resilience.cooldown.note(self.cooldown_key, failure)
                            log.warning(
                                "%s stream rejected (%s, %s); backing off %.1fs (attempt %d/%d)",
                                self.vendor,
                                failure.status_code,
                                failure.kind,
                                delay,
                                attempt,
                                policy.attempts(),
                            )
                            await asyncio.sleep(delay)
                            continue
                        if failure.retryable:
                            resilience.cooldown.note(self.cooldown_key, failure)
                        wait = f" Retry in about {failure.retry_after:.0f}s." if failure.retry_after else ""
                        yield StreamChunk(error=f"{failure.summary()}{wait}", finish_reason="error")
                        return
                    resilience.cooldown.clear(self.cooldown_key)
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
                            # Reasoning is a *separate* channel: DeepSeek/R1 and
                            # OpenRouter send `reasoning_content`, some gateways
                            # `reasoning`, Anthropic-bridges `thinking`.  Yielding
                            # it on its own field is what lets the panel render a
                            # real thinking window instead of faking one.
                            thinking = (
                                delta.get("reasoning_content")
                                or delta.get("reasoning")
                                or delta.get("thinking")
                                or ""
                            )
                            if isinstance(thinking, dict):
                                thinking = thinking.get("text") or thinking.get("content") or ""
                            if thinking:
                                yield StreamChunk(reasoning=str(thinking))
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
                    return
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
            reasoning=_reasoning_of(message),
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


def _reasoning_of(message: dict[str, Any]) -> str:
    """Vendor-neutral extraction of the internal reasoning text.

    DeepSeek/R1 and OpenRouter -> ``reasoning_content``, several gateways ->
    ``reasoning``, Anthropic bridges -> ``thinking`` (string *or* object).
    Returns ``""`` when the model produced none, so callers can branch cheaply.
    """

    for key in ("reasoning_content", "reasoning", "thinking", "reasoning_text"):
        value = message.get(key)
        if isinstance(value, dict):
            value = value.get("text") or value.get("content") or ""
        if isinstance(value, str) and value.strip():
            return value
    return ""


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
