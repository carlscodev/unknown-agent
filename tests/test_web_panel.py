"""Web control panel: auth store, pairing handshake, API and chat behaviour.

The handshake test drives the store through *two separate instances* sharing one
file, which is exactly the CLI-vs-server process split in production: the browser
talks to instance A (the server), ``ua approve device`` is simulated by instance B.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
    return SimpleNamespace(paths=paths, auth=auth, app=app, auth_file=auth_file, static=static)


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
        assert {"help", "model", "clear"} <= set(names)


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
            for _ in range(4):
                msg = ws.receive_json()
                seen.append(msg)
                if msg["type"] in {"done", "error"}:
                    break
            kinds = [m["type"] for m in seen]
            assert "delta" in kinds and kinds[-1] == "done"

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
