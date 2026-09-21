"""Web control panel: auth store, pairing handshake, API and chat behaviour.

The handshake test drives the store through *two separate instances* sharing one
file, which is exactly the CLI-vs-server process split in production: the browser
talks to instance A (the server), ``ua approve device`` is simulated by instance B.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient

from unknown_agent.config.loader import save_settings
from unknown_agent.config.models import ProviderConfig, Settings
from unknown_agent.config.paths import AppPaths
from unknown_agent.web.auth import WebAuthStore
from unknown_agent.web.server import create_app

OWNER = "test-owner-token"


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
        cwd=tmp_path,
    )


@pytest.fixture()
def env(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.ensure_user_dirs()
    settings = Settings()
    settings.providers = {
        "unli": ProviderConfig(
            type="openai-compatible",
            base_url="http://127.0.0.1:9",
            api_key="sk-test",
            model="auto",
        )
    }
    settings.provider = "unli"
    save_settings(settings, paths.user_config_file)

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>UA-PANEL</html>", encoding="utf-8")

    auth_file = paths.config_dir / "web_auth.json"
    auth = WebAuthStore(auth_file)
    data = auth._load()
    data["owner_token"] = OWNER
    auth._save()
    app = create_app(auth=auth, paths=paths, static_dir=static)
    return SimpleNamespace(
        paths=paths, auth=auth, app=app, auth_file=auth_file, static=static, settings=settings
    )


def _client(env) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://panel.test"
    )


# --------------------------------------------------------------------------- #
# auth store + pairing across processes
# --------------------------------------------------------------------------- #
async def test_owner_login_and_session(env) -> None:
    async with _client(env) as c:
        r = await c.get("/api/status")
        assert r.status_code == 401

        r = await c.post("/api/auth/login", json={"token": "wrong"})
        assert r.status_code == 401

        r = await c.post("/api/auth/login", json={"token": OWNER})
        assert r.status_code == 200
        r = await c.get("/api/session")
        assert r.json() == {"authenticated": True, "role": "owner", "device": None}

        r = await c.get("/api/status")
        body = r.json()
        assert body["active"] == "unli"
        unli = next(p for p in body["providers"] if p["name"] == "unli")
        assert unli["model"] == "auto"


async def test_device_pairing_handshake_across_processes(env) -> None:
    """Browser polls server (instance A); the terminal approves via instance B."""
    async with _client(env) as browser:
        r = await browser.post("/api/auth/device/request", json={})
        assert r.status_code == 200
        code = r.json()["code"]
        assert code.isdigit() and len(code) == 6

        # Not approved yet.
        r = await browser.post("/api/auth/device/claim", json={"code": code})
        assert r.status_code == 202

        # The terminal is a *different* store instance on the same file.
        terminal = WebAuthStore(env.auth_file)
        assert code in terminal.pending()
        assert terminal.approve_device(code, name="phone")

        # Server instance sees the approval without a restart and grants a session.
        r = await browser.post("/api/auth/device/claim", json={"code": code})
        assert r.status_code == 200
        r = await browser.get("/api/session")
        assert r.json()["role"] == "device"

        # Device sessions are not owners: management stays forbidden.
        r = await browser.get("/api/devices")
        assert r.status_code == 401

        # Owner revokes through yet another instance; the device is cut off.
        WebAuthStore(env.auth_file).revoke_device(code)
        r = await browser.get("/api/session")
        assert r.json() == {"authenticated": False}


async def test_approve_rejects_bad_code(env) -> None:
    assert WebAuthStore(env.auth_file).approve_device("12ab56") is False
    assert WebAuthStore(env.auth_file).approve_device("123456") is False


async def test_token_rotation_invalidates_owner_sessions(env) -> None:
    async with _client(env) as c:
        r = await c.post("/api/auth/login", json={"token": OWNER})
        assert r.status_code == 200
        r = await c.post("/api/token/rotate", json={})
        new_token = r.json()["token"]
        assert new_token and new_token != OWNER

        # Old token no longer logs in; the fresh one does.
        async with _client(env) as c2:
            r = await c2.post("/api/auth/login", json={"token": OWNER})
            assert r.status_code == 401
            r = await c2.post("/api/auth/login", json={"token": new_token})
            assert r.status_code == 200


# --------------------------------------------------------------------------- #
# chat: commands, provider errors, streaming
# --------------------------------------------------------------------------- #
async def _owner_client(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/auth/login", json={"token": OWNER})
    assert r.status_code == 200


async def test_chat_slash_commands(env) -> None:
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "/status"})
        body = r.json()
        assert "active provider: unli" in body["content"]

        r = await c.post("/api/chat", json={"text": "/providers"})
        assert "* unli" in r.json()["content"]

        r = await c.post("/api/chat", json={"text": "/provider missing"})
        assert "no profile" in r.json()["content"]

        r = await c.post("/api/chat", json={"text": "/provider unli"})
        assert "active provider is now 'unli'" in r.json()["content"]

        r = await c.post("/api/chat", json={"text": "/nonsense"})
        assert "unknown command" in r.json()["content"]

        r = await c.get("/api/commands")
        names = [c_["name"] for c_ in r.json()]
        assert {"help", "model", "clear", "pack"} <= set(names)


async def test_chat_pack_lists_default_skills(env) -> None:
    """The web chat runs with the same skill pack as the CLI/TUI agent."""
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "/pack"})
        content = r.json()["content"]
        assert content.startswith("packs:")
        assert "- " in content  # at least one builtin skill from the default pack


async def test_chat_surfaces_real_provider_error(env) -> None:
    """Port 9 is closed: the panel must surface the failure, never hang or fake it."""
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "hola"})
        assert r.status_code == 502
        assert r.json()["error"]


async def test_chat_streams_via_provider(env, monkeypatch: pytest.MonkeyPatch) -> None:
    class StubProvider:
        supports_streaming = True
        model = "stub-1"
        temperature = 0.2
        max_output_tokens = 512

        def __init__(self, *a, **k) -> None:
            pass

        async def stream(self, request):
            for delta in ("Hola", " desde", " el stub"):
                yield SimpleNamespace(delta=delta, error=None, usage=None, finish_reason=None)

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: StubProvider())

    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "hola"})
        assert r.status_code == 200
        body = r.json()
        assert "".join(body["deltas"]) == "Hola desde el stub"

        # Second turn keeps server-side history: the stub sees two user messages.
        r = await c.post("/api/chat", json={"text": "/clear"})
        assert "cleared" in r.json()["content"]


async def test_websocket_chat(env, monkeypatch: pytest.MonkeyPatch) -> None:
    class StubProvider:
        supports_streaming = True
        model = "stub-1"
        temperature = 0.2
        max_output_tokens = 512

        async def stream(self, request):
            yield SimpleNamespace(delta="ws-ok", error=None, usage=None, finish_reason=None)

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: StubProvider())

    with TestClient(env.app) as client:
        r = client.post("/api/auth/login", json={"token": OWNER})
        assert r.status_code == 200
        with client.websocket_connect("/api/ws") as ws:
            ws.send_json({"text": "ping"})
            seen = []
            for _ in range(20):
                msg = ws.receive_json()
                seen.append(msg)
                if msg["type"] in {"done", "error"}:
                    break
            kinds = [m["type"] for m in seen]
            assert "delta" in kinds and kinds[-1] == "done"
            # mode first, then the attached toolset, before the first token
            assert kinds[0] == "mode"
            assert "tools" in kinds

    # Unauthenticated socket is refused outright.
    from starlette.websockets import WebSocketDisconnect

    with (
        TestClient(env.app) as anon,
        pytest.raises(WebSocketDisconnect),
        anon.websocket_connect("/api/ws") as ws,
    ):
        ws.receive_json()


# --------------------------------------------------------------------------- #
# static SPA + management endpoints
# --------------------------------------------------------------------------- #
async def test_spa_serves_vue_index_and_fallback(env) -> None:
    async with _client(env) as c:
        r = await c.get("/")
        assert "UA-PANEL" in r.text
        r = await c.get("/some/deep/route")
        assert "UA-PANEL" in r.text
        r = await c.get("/api/nope")
        assert r.status_code == 404


async def test_model_switch_persists(env) -> None:
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/model", json={"id": "unli-auto"})
        assert r.status_code == 200
        assert r.json()["model"] == "unli-auto"
        # Persisted to the same config file the TUI/CLI read.
        text = (env.paths.user_config_file).read_text(encoding="utf-8")
        assert "unli-auto" in text


# --------------------------------------------------------------------------- #
# v1.3: uploads, attachments, stop, dashboard
# --------------------------------------------------------------------------- #
async def test_upload_text_is_extracted_into_context(env, monkeypatch) -> None:
    captured = {}

    class StubProvider:
        supports_streaming = False
        model = "stub-1"
        temperature = 0.2
        max_output_tokens = 512

        async def complete(self, request):
            captured["messages"] = request.messages
            return SimpleNamespace(content="got it", error=None)

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: StubProvider())

    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post(
            "/api/upload",
            files={"file": ("notes.txt", b"the vault code is 4242", "text/plain")},
        )
        assert r.status_code == 200
        meta = r.json()
        assert meta["kind"] == "text"

        r = await c.post(
            "/api/chat", json={"text": "what is in the file?", "attachments": [meta["id"]]}
        )
        assert r.status_code == 200
        user_msgs = [m for m in captured["messages"] if m.role == "user"]
        assert "the vault code is 4242" in user_msgs[-1].content


async def test_upload_image_becomes_vision_part(env, monkeypatch) -> None:
    captured = {}

    class StubProvider:
        supports_streaming = False
        model = "stub-1"
        temperature = 0.2
        max_output_tokens = 512

        async def complete(self, request):
            captured["messages"] = request.messages
            return SimpleNamespace(content="nice image", error=None)

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: StubProvider())

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
    )
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/upload", files={"file": ("shot.png", png, "image/png")})
        meta = r.json()
        assert meta["kind"] == "image"
        r = await c.post("/api/chat", json={"text": "describe", "attachments": [meta["id"]]})
        assert r.status_code == 200
        last_user = [m for m in captured["messages"] if m.role == "user"][-1]
        assert last_user.images and last_user.images[0].startswith("data:image/png;base64,")


async def test_upload_over_cap_rejected(env) -> None:
    async with _client(env) as c:
        await _owner_client(c)
        # shrink the cap through the dashboard, then exceed it
        r = await c.post("/api/dashboard/files", json={"max_upload_mb": 1})
        assert r.status_code == 200
        big = b"x" * (1024 * 1024 + 10)
        r = await c.post(
            "/api/upload", files={"file": ("big.bin", big, "application/octet-stream")}
        )
        assert r.status_code == 413


async def test_stop_cancels_generation(env, monkeypatch) -> None:
    class SlowProvider:
        supports_streaming = True
        model = "stub-1"
        temperature = 0.2
        max_output_tokens = 512

        async def stream(self, request):
            for i in range(200):
                import asyncio as _a

                await _a.sleep(0.05)
                yield SimpleNamespace(delta=f"tok{i} ", error=None, usage=None, finish_reason=None)

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: SlowProvider())

    with TestClient(env.app) as client:
        r = client.post("/api/auth/login", json={"token": OWNER})
        assert r.status_code == 200
        with client.websocket_connect("/api/ws") as ws:
            ws.send_json({"text": "long one"})
            for _ in range(20):  # mode/tools/phase announcements precede the stream
                first = ws.receive_json()
                if first["type"] == "delta":
                    break
            assert first["type"] == "delta"
            ws.send_json({"type": "stop"})
            kinds = []
            for _ in range(10):
                m = ws.receive_json()
                kinds.append(m["type"])
                if "stopped" in kinds:
                    break
            assert "stopped" in kinds


async def test_dashboard_mcp_and_providers(env) -> None:
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post(
            "/api/dashboard/mcp/add",
            json={"name": "fs", "command": "uvx", "args": ["mcp-server-fs"]},
        )
        assert r.status_code == 200

        r = await c.get("/api/dashboard")
        srv = next(s for s in r.json()["mcp"]["servers"] if s["name"] == "fs")
        assert srv["enabled"] is True

        r = await c.post("/api/dashboard/mcp/toggle", json={"name": "fs", "enabled": False})
        assert r.json()["enabled"] is False
        # persisted to the real config file
        text = env.paths.user_config_file.read_text(encoding="utf-8")
        assert "mcp" in text and "fs" in text

        r = await c.post(
            "/api/dashboard/provider",
            json={"name": "backup", "type": "openai-compatible", "model": "gpt-4o-mini"},
        )
        assert r.status_code == 200
        r = await c.post("/api/dashboard/provider/remove", json={"name": "backup"})
        assert r.status_code == 200

        r = await c.post("/api/dashboard/agent", json={"mode": "plan", "max_steps": 21})
        assert r.json()["mode"] == "plan" and r.json()["max_steps"] == 21


# --------------------------------------------------------------------------- #
# full agent mode: real Runner with tools + live artifact preview
# --------------------------------------------------------------------------- #
class _StubRunner:
    """Stands in for the real Runner; exercises the hook -> event wiring."""

    instances: ClassVar[list] = []

    def __init__(self, loaded, request=None, hooks=None):
        self.request = request
        self.hooks = hooks
        _StubRunner.instances.append(self)

    async def setup(self):
        self.hooks.on_thought("planning the landing page")
        self.hooks.on_tool_start(SimpleNamespace(name="write_file"))
        self.hooks.on_tool_end(
            SimpleNamespace(name="write_file"),
            SimpleNamespace(ok=True, summary="wrote demo/index.html"),
        )

    async def execute(self):
        step = SimpleNamespace(decision=SimpleNamespace(answer="Listo: escribi demo/index.html"))
        return SimpleNamespace(
            summary="boilerplate summary",
            outcome=SimpleNamespace(steps=[step]),
            ok=True,
            blockers=[],
            status="completed",
            files_changed=["demo/index.html"],
            state=SimpleNamespace(model="auto", provider="unli"),
        )


async def test_agent_mode_runs_real_runner_and_emits_artifact(env, monkeypatch) -> None:
    _StubRunner.instances.clear()
    import unknown_agent.core.runner as runner_mod

    monkeypatch.setattr(runner_mod, "Runner", _StubRunner)

    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post(
            "/api/chat",
            json={"text": "crea una landing", "agent": True, "provider": "unli", "model": "auto"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == "Listo: escribi demo/index.html"
        assert body["artifacts"] == [
            {
                "type": "artifact",
                "kind": "html",
                "url": "/api/files/demo/index.html",
                "name": "demo/index.html",
            }
        ]
        # provider/model chosen in the composer are passed to the Runner
        assert _StubRunner.instances[0].request.provider == "unli"
        assert _StubRunner.instances[0].request.model == "auto"
        # the real Runner received the task and auto-approve for non-interactive use
        assert len(_StubRunner.instances) == 1
        assert _StubRunner.instances[0].request.task == "crea una landing"
        assert _StubRunner.instances[0].request.yes is True

        # and the preview URL actually serves what the agent wrote
        demo = env.paths.cwd / "demo"
        demo.mkdir()
        (demo / "index.html").write_text("<html>AGENT-BUILT</html>", encoding="utf-8")
        r = await c.get("/api/files/demo/index.html")
        assert r.status_code == 200
        assert "AGENT-BUILT" in r.text


async def test_agent_mode_blocked_run_reports_blockers(env, monkeypatch) -> None:
    import unknown_agent.core.runner as runner_mod

    class _BlockedRunner(_StubRunner):
        async def setup(self):
            return None

        async def execute(self):
            return SimpleNamespace(
                summary="",
                ok=False,
                blockers=["verification failed"],
                status="blocked",
                files_changed=[],
                state=SimpleNamespace(model="", provider=""),
            )

    monkeypatch.setattr(runner_mod, "Runner", _BlockedRunner)
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "arregla el bug", "agent": True})
        assert r.status_code == 502
        assert "verification failed" in r.json()["error"]


async def test_files_endpoint_requires_auth_and_blocks_traversal(env) -> None:
    (env.paths.cwd / "secret.txt").write_text("nope", encoding="utf-8")
    outside = env.paths.cwd.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    async with _client(env) as c:
        r = await c.get("/api/files/secret.txt")
        assert r.status_code == 401  # no session

        await _owner_client(c)
        r = await c.get("/api/files/secret.txt")
        assert r.status_code == 200
        r = await c.get("/api/files/..%2Foutside.txt")
        assert r.status_code == 404  # traversal stays rooted at cwd
        r = await c.get("/api/files/missing.txt")
        assert r.status_code == 404


async def test_agent_mode_emits_image_and_file_artifacts(env, monkeypatch) -> None:
    import unknown_agent.core.runner as runner_mod

    class _ImageRunner(_StubRunner):
        async def setup(self):
            return None

        async def execute(self):
            return SimpleNamespace(
                summary="listo",
                outcome=None,
                ok=True,
                blockers=[],
                status="completed",
                files_changed=["assets/logo.png", "notes.md", "data.bin"],
                state=SimpleNamespace(model="auto", provider="unli"),
            )

    monkeypatch.setattr(runner_mod, "Runner", _ImageRunner)
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "crea el logo", "agent": True})
        kinds = [a["kind"] for a in r.json()["artifacts"]]
        names = [a["name"] for a in r.json()["artifacts"]]
        assert kinds == ["image", "file"]
        assert names == ["assets/logo.png", "notes.md"]  # binary junk gets no card


async def test_chat_provider_and_model_override(env, monkeypatch) -> None:
    captured = {}

    class StubProvider:
        supports_streaming = False
        model = "stub-1"
        temperature = 0.2
        max_output_tokens = 512

        async def complete(self, request):
            captured["model"] = self.model
            return SimpleNamespace(content="ok", error=None)

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: StubProvider())
    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post(
            "/api/chat", json={"text": "hola, todo bien?", "provider": "unli", "model": "mini-9"}
        )
        assert r.status_code == 200
        assert captured["model"] == "mini-9"


async def test_auto_route_sends_questions_to_streaming_chat(env, monkeypatch) -> None:
    called = {}

    class StubProvider:
        supports_streaming = False
        model = "stub-1"
        temperature = 0.2
        max_output_tokens = 512

        async def complete(self, request):
            called["hit"] = True
            return SimpleNamespace(content="just chatting", error=None)

    import unknown_agent.core.runner as runner_mod

    def _boom(*_a, **_k):  # agent path must not be taken
        raise AssertionError("full agent should not run for a question")

    monkeypatch.setattr(runner_mod, "Runner", _boom)
    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: StubProvider())

    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "what can you do with files?"})
        assert r.status_code == 200
        assert r.json()["content"] == "just chatting"
        assert called.get("hit") is True


# --------------------------------------------------------------------------- #
# tool-enabled streaming chat (the fix for "no tengo herramientas")
# --------------------------------------------------------------------------- #
def _tooling_provider(monkeypatch: pytest.MonkeyPatch) -> type:
    """A provider double that thinks, calls a tool, then answers."""

    from unknown_agent.core.models import ToolCall
    from unknown_agent.providers.base import Usage

    class ToolingProvider:
        supports_streaming = True
        supports_tools = True
        model = "stub-tools"
        temperature = 0.2
        max_output_tokens = 512
        requests: ClassVar[list[Any]] = []

        def __init__(self, *a, **k) -> None:
            self.turn = 0

        async def stream(self, request):
            ToolingProvider.requests.append(request)
            self.turn += 1
            if self.turn == 1:
                yield SimpleNamespace(
                    delta="", reasoning="voy a escribir el archivo", tool_call=None,
                    error=None, usage=None, finish_reason=None,
                )
                yield SimpleNamespace(
                    delta="", reasoning="", error=None, usage=None,
                    finish_reason="tool_calls",
                    tool_call=ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "demo/index.html", "content": "<html>LIVE</html>"},
                    ),
                )
            else:
                yield SimpleNamespace(
                    delta="Listo", reasoning="", tool_call=None, error=None,
                    usage=Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
                    finish_reason="stop",
                )

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: ToolingProvider())
    ToolingProvider.requests.clear()
    return ToolingProvider


def _collect_ws(ws, text: str, limit: int = 60):
    ws.send_json({"text": text})
    seen = []
    for _ in range(limit):
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] in {"done", "error"}:
            break
    return seen


async def test_chat_runs_real_tools_and_streams_every_phase(env, monkeypatch) -> None:
    """A build request must write the file on this host, not apologise."""

    _tooling_provider(monkeypatch)

    with TestClient(env.app) as client:
        client.post("/api/auth/login", json={"token": OWNER})
        with client.websocket_connect("/api/ws") as ws:
            seen = _collect_ws(ws, "crea una landing page")

    kinds = [m["type"] for m in seen]
    assert "tools" in kinds
    assert "reasoning" in kinds, "internal thinking must stream on its own channel"
    assert "tool" in kinds
    assert "artifact" in kinds
    assert kinds[-1] == "done"

    # the tool really ran through the policy-checked executor
    written = env.paths.cwd / "demo" / "index.html"
    assert written.is_file()
    assert "LIVE" in written.read_text(encoding="utf-8")

    starts = [m for m in seen if m["type"] == "tool" and m["status"] == "start"]
    ends = [m for m in seen if m["type"] == "tool" and m["status"] == "end"]
    assert starts and ends
    assert starts[0]["name"] == "write_file"
    assert ends[0]["ok"] is True
    assert ends[0]["duration_ms"] >= 0

    artifact = [m for m in seen if m["type"] == "artifact"][0]
    assert artifact["url"] == "/api/files/demo/index.html"
    assert artifact["kind"] == "html"

    done = seen[-1]
    assert done["content"] == "Listo"
    assert done["timing"]["total_ms"] >= 0
    assert done["tools"][0]["name"] == "write_file"


async def test_system_prompt_declares_the_tools(env, monkeypatch) -> None:
    """The model is told it has tools, so it stops inventing excuses."""

    provider = _tooling_provider(monkeypatch)

    async with _client(env) as c:
        await _owner_client(c)
        r = await c.post("/api/chat", json={"text": "crea una landing page"})
        assert r.status_code == 200

    request = provider.requests[0]
    system = [m for m in request.messages if m.role == "system"][0].content
    assert "write_file" in system
    assert "NEVER" in system
    assert str(env.paths.cwd) in system
    # the model receives real schemas, not a description of them
    assert any(tool.name == "write_file" for tool in request.tools)


async def test_artifact_is_previewable_with_the_right_media_type(env, monkeypatch) -> None:
    _tooling_provider(monkeypatch)

    async with _client(env) as c:
        await _owner_client(c)
        assert (await c.post("/api/chat", json={"text": "crea una landing"})).status_code == 200
        r = await c.get("/api/files/demo/index.html")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "LIVE" in r.text
        # and it can be downloaded instead of previewed
        r = await c.get("/api/files/demo/index.html?download=1")
        assert "attachment" in r.headers["content-disposition"]


async def test_status_reports_the_attached_toolset(env, monkeypatch) -> None:
    _tooling_provider(monkeypatch)

    async with _client(env) as c:
        await _owner_client(c)
        await c.post("/api/chat", json={"text": "hola"})
        status = await c.get("/api/status")
        assert status.status_code == 200
        body = status.json()
        assert body["tool_count"] > 10
        assert "write_file" in body["tools"]
        assert body["cwd"] == str(env.paths.cwd)


async def test_stop_flag_ends_the_tool_loop_between_steps(env, monkeypatch) -> None:
    """Stop is cooperative: the flag stops the loop at the next step boundary."""

    from unknown_agent.core.models import ToolCall
    from unknown_agent.web.agent_loop import WebAgent

    class LoopingProvider:
        supports_streaming = True
        supports_tools = True
        model = "stub-loop"
        temperature = 0.2
        max_output_tokens = 512
        turns = 0

        async def stream(self, request):
            LoopingProvider.turns += 1
            yield SimpleNamespace(
                delta="", reasoning="", error=None, usage=None, finish_reason="tool_calls",
                tool_call=ToolCall(id="c", name="list_files", arguments={"path": "."}),
            )

    import unknown_agent.providers.registry as registry

    monkeypatch.setattr(registry, "build_provider", lambda *_, **__: LoopingProvider())

    agent = WebAgent(settings=env.settings, paths=env.paths)
    await agent.setup()

    seen: list[dict[str, Any]] = []

    async def emit(msg: dict[str, Any]) -> None:
        seen.append(msg)

    stop_at = {"n": 0}

    def should_stop() -> bool:
        stop_at["n"] += 1
        return stop_at["n"] > 2

    result = await agent.chat(task="recorre el proyecto", emit=emit, should_stop=should_stop)
    assert LoopingProvider.turns == 2, "the loop must stop at the next boundary"
    assert len([m for m in seen if m["type"] == "tool" and m["status"] == "end"]) == 2
    assert result.steps == 2
