"""Strict JSON-RPC 2.0 dispatcher and concurrent NDJSON stdio transport."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_RPC_MESSAGE_BYTES = 1_048_576
MAX_RPC_TIMEOUT_SECONDS = 300.0


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: str = Field(pattern=r"^2\.0$")
    id: int | str | None = None
    method: str = Field(min_length=1, max_length=256)
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


Handler = Callable[[dict[str, Any]], Awaitable[Any]]
NotificationSink = Callable[[dict[str, Any]], Awaitable[None]]


class JsonRpcServer:
    """Method registry with bounded execution and protocol-safe errors."""

    def __init__(self, *, timeout_seconds: float = 300, max_in_flight: int = 32) -> None:
        if not 0 < timeout_seconds <= MAX_RPC_TIMEOUT_SECONDS:
            raise ValueError("RPC timeout must be greater than zero and at most 300 seconds")
        self.timeout_seconds = timeout_seconds
        self.handlers: dict[str, Handler] = {}
        self._limit = asyncio.Semaphore(max_in_flight)
        self.notifications: NotificationSink | None = None

    def method(self, name: str, handler: Handler) -> None:
        if not name or name in self.handlers:
            raise ValueError(f"RPC method is invalid or already registered: {name!r}")
        self.handlers[name] = handler

    async def dispatch(self, raw: bytes | str) -> dict[str, Any] | None:
        encoded = raw.encode() if isinstance(raw, str) else raw
        if len(encoded) > MAX_RPC_MESSAGE_BYTES:
            return self._error(None, -32600, "request exceeds 1048576 bytes")
        try:
            value = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._error(None, -32700, "parse error", str(exc))
        try:
            request = JsonRpcRequest.model_validate(value)
        except ValidationError as exc:
            identifier = value.get("id") if isinstance(value, dict) else None
            return self._error(identifier, -32600, "invalid request", exc.errors())
        handler = self.handlers.get(request.method)
        if handler is None:
            if request.id is None:
                return None
            return self._error(request.id, -32601, "method not found")
        try:
            async with self._limit:
                result = await asyncio.wait_for(
                    handler(request.params), timeout=self.timeout_seconds
                )
        except JsonRpcError as exc:
            if request.id is None:
                return None
            return self._error(request.id, exc.code, exc.message, exc.data)
        except TimeoutError:
            if request.id is None:
                return None
            return self._error(request.id, -32001, "request timed out")
        except asyncio.CancelledError:
            if request.id is None:
                return None
            return self._error(request.id, -32800, "request cancelled")
        except Exception as exc:
            if request.id is None:
                return None
            return self._error(request.id, -32603, "internal error", type(exc).__name__)
        if request.id is None:
            return None
        return {"jsonrpc": "2.0", "id": request.id, "result": result}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.notifications is not None:
            await self.notifications({"jsonrpc": "2.0", "method": method, "params": params})

    @staticmethod
    def _error(identifier: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": identifier, "error": error}

    async def serve_stdio(self) -> None:
        """Serve newline-delimited JSON-RPC without polluting stdout."""

        writer_lock = asyncio.Lock()
        tasks: set[asyncio.Task[None]] = set()

        async def write(message: dict[str, Any]) -> None:
            encoded = (
                json.dumps(
                    message,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            )
            async with writer_lock:
                await asyncio.to_thread(sys.stdout.write, encoded)
                await asyncio.to_thread(sys.stdout.flush)

        self.notifications = write

        async def process(line: bytes) -> None:
            response = await self.dispatch(line)
            if response is not None:
                await write(response)

        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            task = asyncio.create_task(process(line))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
