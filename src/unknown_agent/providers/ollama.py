"""Ollama provider: local models with zero credentials.

Used for air-gapped machines, privacy-sensitive repositories and cost control.
Tool calling is supported on models that advertise it; otherwise the agent falls
back to its textual tool protocol (see ``prompts/tool_text_protocol``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from unknown_agent.core.models import Message, ToolCall
from unknown_agent.providers.base import FinishReason, ModelProvider, ModelRequest, ModelResponse, StreamChunk, Usage

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class OllamaProvider(ModelProvider):
    name = "ollama"
    supports_json_mode = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = (self.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Content-Type": "application/json", **self.headers},
                timeout=httpx.Timeout(self.request_timeout_s, connect=self.connect_timeout_s),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": request.temperature if request.temperature is not None else self.temperature}
        if request.max_output_tokens:
            options["num_predict"] = request.max_output_tokens
        messages: list[dict[str, Any]] = []
        for m in request.messages:
            entry: dict[str, Any] = {"role": m.role if m.role != "tool" else "tool", "content": m.content}
            if m.images:
                # Ollama's dialect: raw base64 (no data-URL header) in an "images" array.
                encoded = [url.partition(",")[2] for url in m.images if url.startswith("data:")]
                if encoded:
                    entry["images"] = encoded
            messages.append(entry)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": options,
        }
        if request.tools and self.supports_tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
                }
                for tool in request.tools
            ]
        if request.json_mode:
            payload["format"] = "json"
        return payload

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = self.timed()
        try:
            response = await self.client.post("/api/chat", json=self._payload(request, stream=False))
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            hint = "Start the server with `ollama serve` and pull the model with `ollama pull <model>`."
            return ModelResponse(error=f"HTTP {exc.response.status_code}: {body}", finish_reason="error", provider=self.name, model=self.model)
        except httpx.HTTPError as exc:
            return ModelResponse(
                error=f"Ollama is unreachable at {self.base_url}: {exc}",
                finish_reason="error",
                provider=self.name,
                model=self.model,
            )
        body = response.json()
        message = body.get("message") or {}
        calls = [
            ToolCall(
                id=ToolCall.new((item.get("function") or {}).get("name", "tool")).id,
                name=(item.get("function") or {}).get("name", ""),
                arguments=(item.get("function") or {}).get("arguments") or {},
            )
            for item in (message.get("tool_calls") or [])
        ]
        prompt_tokens = int(body.get("prompt_eval_count", 0))
        completion_tokens = int(body.get("eval_count", 0))
        return ModelResponse(
            content=message.get("content") or "",
            tool_calls=[call for call in calls if call.name],
            finish_reason="tool_calls" if calls else ("length" if body.get("done_reason") == "length" else "stop"),
            usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=prompt_tokens + completion_tokens),
            model=body.get("model", self.model),
            provider=self.name,
            latency_ms=(self.timed() - started) * 1000,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        try:
            async with self.client.stream("POST", "/api/chat", json=self._payload(request, stream=True)) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:300]
                    yield StreamChunk(error=f"HTTP {response.status_code}: {body}", finish_reason="error")
                    return
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = event.get("message") or {}
                    if message.get("content"):
                        yield StreamChunk(delta=message["content"])
                    for item in message.get("tool_calls") or []:
                        function = item.get("function") or {}
                        yield StreamChunk(tool_call=ToolCall(id=ToolCall.new(function.get("name", "tool")).id, name=function.get("name", ""), arguments=function.get("arguments") or {}))
                    if event.get("done"):
                        yield StreamChunk(
                            finish_reason="stop",
                            usage=Usage(
                                prompt_tokens=int(event.get("prompt_eval_count", 0)),
                                completion_tokens=int(event.get("eval_count", 0)),
                            ),
                        )
        except httpx.HTTPError as exc:
            yield StreamChunk(error=f"Ollama is unreachable at {self.base_url}: {exc}", finish_reason="error")

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["note"] = "local, no credentials"
        return info


def build(**kwargs: Any) -> ModelProvider:
    return OllamaProvider(**kwargs)


__all__ = ["OllamaProvider", "build"]
