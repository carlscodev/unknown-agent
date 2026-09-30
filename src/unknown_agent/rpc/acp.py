"""ACP v1 adapter over the existing Unknown Agent runner."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from unknown_agent.config.loader import LoadedSettings
from unknown_agent.core.runner import RunRequest, run_task
from unknown_agent.observability.events import Event
from unknown_agent.rpc.jsonrpc import JsonRpcError, JsonRpcServer
from unknown_agent.version import __version__


class InitializeParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    protocol_version: int = Field(alias="protocolVersion")
    client_capabilities: dict[str, Any] = Field(default_factory=dict, alias="clientCapabilities")
    client_info: dict[str, Any] = Field(default_factory=dict, alias="clientInfo")


class NewSessionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list, alias="mcpServers")
    additional_directories: list[str] = Field(default_factory=list, alias="additionalDirectories")


class LoadSessionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(alias="sessionId")
    cwd: str | None = None
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list, alias="mcpServers")


class ContentBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["text", "resource", "resource_link", "image", "audio"]
    text: str | None = Field(default=None, max_length=200_000)


class PromptParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(alias="sessionId")
    prompt: list[ContentBlock] = Field(min_length=1, max_length=128)


class CancelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(alias="sessionId")


class ModeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(alias="sessionId")
    mode_id: Literal["auto", "plan", "react"] = Field(alias="modeId")


class AcpSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    cwd: str
    mode: Literal["auto", "plan", "react"] = "auto"
    created_at: float
    updated_at: float
    last_run_id: str | None = None


class AcpSessionStore:
    """Atomic, project-local ACP session metadata."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def all(self) -> dict[str, AcpSession]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not isinstance(payload.get("sessions"), dict):
            raise ValueError("invalid ACP session store")
        return {key: AcpSession.model_validate(value) for key, value in payload["sessions"].items()}

    def get(self, session_id: str) -> AcpSession | None:
        return self.all().get(session_id)

    def put(self, session: AcpSession) -> None:
        sessions = self.all()
        sessions[session.id] = session
        payload = {
            "schema_version": 1,
            "sessions": {
                key: value.model_dump(mode="json") for key, value in sorted(sessions.items())
            },
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)


class AcpAgent:
    """ACP lifecycle and streaming mapped to the canonical runner."""

    def __init__(
        self,
        loaded_factory: Callable[[], LoadedSettings],
        *,
        timeout_seconds: float = 300,
    ) -> None:
        self.loaded_factory = loaded_factory
        loaded = loaded_factory()
        self.project_root = loaded.paths.cwd.resolve()
        self.store = AcpSessionStore(loaded.paths.project_dir / "acp-sessions.json")
        self.rpc = JsonRpcServer(timeout_seconds=timeout_seconds)
        self.active: dict[str, asyncio.Task[Any]] = {}
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self.initialized = False
        self._register()

    def _register(self) -> None:
        self.rpc.method("initialize", self.initialize)
        self.rpc.method("session/new", self.new_session)
        self.rpc.method("session/load", self.load_session)
        self.rpc.method("session/prompt", self.prompt)
        self.rpc.method("session/cancel", self.cancel)
        self.rpc.method("session/set_mode", self.set_mode)
        self.rpc.method("unknown-agent/status", self.status)

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        value = self._validate(InitializeParams, params)
        if value.protocol_version != 1:
            raise JsonRpcError(-32000, "unsupported ACP protocol version", {"supported": [1]})
        self.initialized = True
        return {
            "protocolVersion": 1,
            "agentInfo": {
                "name": "unknown-agent",
                "title": "Unknown Agent",
                "version": __version__,
            },
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": False, "sse": False},
                "sessionCapabilities": {"modes": True},
            },
            "authMethods": [],
        }

    async def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_initialized()
        value = self._validate(NewSessionParams, params)
        if value.mcp_servers:
            raise JsonRpcError(-32602, "client-supplied MCP servers are not supported")
        cwd = self._bounded_cwd(value.cwd)
        for directory in value.additional_directories:
            self._bounded_cwd(directory)
        now = time.time()
        session = AcpSession(
            id=f"sess_{uuid.uuid4().hex}",
            cwd=str(cwd),
            created_at=now,
            updated_at=now,
        )
        self.store.put(session)
        return {"sessionId": session.id, "modes": self._modes(), "models": {"availableModels": []}}

    async def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_initialized()
        value = self._validate(LoadSessionParams, params)
        if value.mcp_servers:
            raise JsonRpcError(-32602, "client-supplied MCP servers are not supported")
        session = self.store.get(value.session_id)
        if session is None:
            raise JsonRpcError(-32002, "session not found")
        if value.cwd is not None:
            self._bounded_cwd(value.cwd)
        return {"sessionId": session.id, "modes": self._modes(session.mode)}

    async def set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        value = self._validate(ModeParams, params)
        session = self._session(value.session_id)
        updated = session.model_copy(update={"mode": value.mode_id, "updated_at": time.time()})
        self.store.put(updated)
        await self._update(
            session.id, {"sessionUpdate": "mode_change", "currentModeId": value.mode_id}
        )
        return {}

    async def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_initialized()
        value = self._validate(PromptParams, params)
        session = self._session(value.session_id)
        if session.id in self.active:
            raise JsonRpcError(-32003, "session already has an active prompt")
        text = "\n".join(block.text or "" for block in value.prompt if block.type == "text").strip()
        if not text:
            raise JsonRpcError(-32602, "prompt must contain a non-empty text block")
        task = asyncio.current_task()
        assert task is not None
        self.active[session.id] = task
        loop = asyncio.get_running_loop()

        def sink(event: Event) -> None:
            update = self._event_update(event)
            if update is not None:
                task = loop.create_task(self._update(session.id, update))
                self._notification_tasks.add(task)
                task.add_done_callback(self._notification_tasks.discard)

        await self._update(
            session.id,
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )
        try:
            loaded = self.loaded_factory()
            request = RunRequest(
                task=text,
                mode=session.mode,
                kind="plan" if session.mode == "plan" else "run",
                read_only=session.mode == "plan",
                yes=False,
                non_interactive=True,
                metadata={
                    "acp_session_id": session.id,
                    "previous_acp_run_id": session.last_run_id or "",
                },
            )
            result = await run_task(loaded, request, event_sink=sink)
            self.store.put(
                session.model_copy(
                    update={
                        "last_run_id": result.run_id,
                        "updated_at": time.time(),
                    }
                )
            )
            if result.summary:
                await self._update(
                    session.id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": result.summary},
                    },
                )
            return {"stopReason": "end_turn" if result.ok else "refusal"}
        except asyncio.CancelledError:
            return {"stopReason": "cancelled"}
        finally:
            self.active.pop(session.id, None)

    async def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        value = self._validate(CancelParams, params)
        self._session(value.session_id)
        task = self.active.get(value.session_id)
        if task is not None:
            task.cancel()
        return {}

    async def status(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": 1,
            "initialized": self.initialized,
            "sessions": len(self.store.all()),
            "active": sorted(self.active),
            "maxTimeoutSeconds": self.rpc.timeout_seconds,
        }

    async def _update(self, session_id: str, update: dict[str, Any]) -> None:
        await self.rpc.notify("session/update", {"sessionId": session_id, "update": update})

    @staticmethod
    def _event_update(event: Event) -> dict[str, Any] | None:
        if event.type == "thought" and event.message:
            return {
                "sessionUpdate": "thought_message_chunk",
                "content": {"type": "text", "text": event.message},
            }
        if event.type in {"tool.started", "tool.requested"}:
            return {
                "sessionUpdate": "tool_call",
                "toolCallId": str(event.data.get("call_id") or uuid.uuid4().hex),
                "title": event.message or str(event.data.get("tool", "tool")),
                "kind": "execute",
                "status": "in_progress",
            }
        if event.type in {"tool.finished", "tool.failed", "tool.denied"}:
            return {
                "sessionUpdate": "tool_call_update",
                "toolCallId": str(event.data.get("call_id") or event.data.get("tool", "tool")),
                "status": "completed" if event.type == "tool.finished" else "failed",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": event.message}}
                ],
            }
        if event.type == "message" and event.message:
            return {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": event.message},
            }
        return None

    def _session(self, session_id: str) -> AcpSession:
        session = self.store.get(session_id)
        if session is None:
            raise JsonRpcError(-32002, "session not found")
        return session

    def _bounded_cwd(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_dir() or not path.is_relative_to(self.project_root):
            raise JsonRpcError(-32602, "cwd must be an existing directory inside the project")
        return path

    def _require_initialized(self) -> None:
        if not self.initialized:
            raise JsonRpcError(-32000, "initialize must be called first")

    @staticmethod
    def _validate(model: type[BaseModel], params: dict[str, Any]) -> Any:
        try:
            return model.model_validate(params)
        except ValidationError as exc:
            raise JsonRpcError(-32602, "invalid params", exc.errors()) from exc

    @staticmethod
    def _modes(current: str = "auto") -> dict[str, Any]:
        return {
            "currentModeId": current,
            "availableModes": [
                {"id": "auto", "name": "Auto", "description": "Plan and execute"},
                {"id": "plan", "name": "Plan", "description": "Read-only planning"},
                {"id": "react", "name": "React", "description": "Direct execution"},
            ],
        }
