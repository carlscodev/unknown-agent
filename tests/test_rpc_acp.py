from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unknown_agent.config.loader import load_settings
from unknown_agent.observability.events import Event
from unknown_agent.rpc import AcpAgent, JsonRpcError, JsonRpcServer


@pytest.mark.asyncio
async def test_jsonrpc_dispatch_contract_and_errors() -> None:
    server = JsonRpcServer(timeout_seconds=1)

    async def echo(params: dict[str, object]) -> dict[str, object]:
        return params

    server.method("echo", echo)
    response = await server.dispatch(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "echo", "params": {"x": 1}})
    )
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}
    assert (await server.dispatch("{"))["error"]["code"] == -32700  # type: ignore[index]
    missing = await server.dispatch(json.dumps({"jsonrpc": "2.0", "id": "x", "method": "missing"}))
    assert missing is not None and missing["error"]["code"] == -32601
    assert await server.dispatch(json.dumps({"jsonrpc": "2.0", "method": "missing"})) is None


@pytest.mark.asyncio
async def test_jsonrpc_timeout_never_exceeds_five_minutes() -> None:
    with pytest.raises(ValueError, match="at most 300"):
        JsonRpcServer(timeout_seconds=301)
    server = JsonRpcServer(timeout_seconds=0.01)

    async def slow(_params: dict[str, object]) -> None:
        await asyncio.sleep(1)

    server.method("slow", slow)
    response = await server.dispatch(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slow"}))
    assert response is not None and response["error"]["code"] == -32001


def agent(tmp_path: Path) -> AcpAgent:
    return AcpAgent(lambda: load_settings(tmp_path), timeout_seconds=5)


@pytest.mark.asyncio
async def test_acp_initialize_session_lifecycle_and_persistence(tmp_path: Path) -> None:
    acp = agent(tmp_path)
    with pytest.raises(JsonRpcError, match="initialize"):
        await acp.new_session({"cwd": str(tmp_path), "mcpServers": []})
    initialized = await acp.initialize(
        {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "test"}}
    )
    assert initialized["protocolVersion"] == 1
    assert initialized["agentCapabilities"]["loadSession"] is True
    created = await acp.new_session({"cwd": str(tmp_path), "mcpServers": []})
    session_id = created["sessionId"]
    assert session_id.startswith("sess_")

    restarted = agent(tmp_path)
    await restarted.initialize({"protocolVersion": 1})
    loaded = await restarted.load_session({"sessionId": session_id, "mcpServers": []})
    assert loaded["sessionId"] == session_id
    assert (tmp_path / ".unknown" / "acp-sessions.json").is_file()


@pytest.mark.asyncio
async def test_acp_rejects_unsupported_version_and_outside_cwd(tmp_path: Path) -> None:
    acp = agent(tmp_path)
    with pytest.raises(JsonRpcError, match="unsupported"):
        await acp.initialize({"protocolVersion": 2})
    await acp.initialize({"protocolVersion": 1})
    with pytest.raises(JsonRpcError, match="inside the project"):
        await acp.new_session({"cwd": str(tmp_path.parent), "mcpServers": []})


@pytest.mark.asyncio
async def test_acp_prompt_streams_runner_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    acp = agent(tmp_path)
    notifications: list[dict[str, object]] = []

    async def notify(message: dict[str, object]) -> None:
        notifications.append(message)

    acp.rpc.notifications = notify
    await acp.initialize({"protocolVersion": 1})
    created = await acp.new_session({"cwd": str(tmp_path), "mcpServers": []})

    async def fake_run(loaded: object, request: object, **kwargs: object) -> object:
        sink = kwargs["event_sink"]
        sink(Event(type="thought", message="checking"))
        sink(Event(type="message", message="streamed answer"))
        await asyncio.sleep(0)
        return SimpleNamespace(ok=True, run_id="run-1", summary="done")

    monkeypatch.setattr("unknown_agent.rpc.acp.run_task", fake_run)
    result = await acp.prompt(
        {
            "sessionId": created["sessionId"],
            "prompt": [{"type": "text", "text": "inspect this project"}],
        }
    )
    await asyncio.sleep(0)
    assert result == {"stopReason": "end_turn"}
    updates = [item["params"]["update"]["sessionUpdate"] for item in notifications]  # type: ignore[index]
    assert "user_message_chunk" in updates
    assert "thought_message_chunk" in updates
    assert "agent_message_chunk" in updates


@pytest.mark.asyncio
async def test_acp_cancel_stops_active_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    acp = agent(tmp_path)
    await acp.initialize({"protocolVersion": 1})
    created = await acp.new_session({"cwd": str(tmp_path), "mcpServers": []})
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_run(*args: object, **kwargs: object) -> object:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    monkeypatch.setattr("unknown_agent.rpc.acp.run_task", fake_run)
    prompt = asyncio.create_task(
        acp.prompt(
            {
                "sessionId": created["sessionId"],
                "prompt": [{"type": "text", "text": "wait"}],
            }
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await acp.cancel({"sessionId": created["sessionId"]})
    assert await prompt == {"stopReason": "cancelled"}
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_mode_change_is_durable(tmp_path: Path) -> None:
    acp = agent(tmp_path)
    await acp.initialize({"protocolVersion": 1})
    created = await acp.new_session({"cwd": str(tmp_path), "mcpServers": []})
    await acp.set_mode({"sessionId": created["sessionId"], "modeId": "plan"})
    assert acp.store.get(created["sessionId"]).mode == "plan"  # type: ignore[union-attr]
