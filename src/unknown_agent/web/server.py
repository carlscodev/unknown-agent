"""FastAPI backend for the web control panel.

Design notes
------------
* **Auth** — two ways in: the owner token printed by ``ua web`` (or
  ``ua web --show-token``), or device pairing: the browser shows a 6-digit code,
  the terminal approves it with ``ua approve device <code>``.  Sessions are
  HttpOnly cookies; the state file is the IPC channel (see :mod:`web.auth`).
* **Chat** — one WebSocket per browser; the server resolves the *active* provider
  profile from the same settings the TUI/CLI use, so ``/provider`` and ``/model``
  here take effect everywhere.  Streaming goes through the provider's own
  ``stream()`` (OpenAI/Anthropic SSE, Ollama chunks) exactly like the TUI path.
* **Static** — the built Vue 3 app lives in ``web/static`` and is served with an
  SPA fallback; ``/api`` is the only non-SPA prefix.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from unknown_agent.config.loader import load_settings, save_settings
from unknown_agent.config.models import Settings
from unknown_agent.config.paths import AppPaths
from unknown_agent.core.models import Message
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import ModelRequest
from unknown_agent.tui.provider_status import active_profile_name
from unknown_agent.version import __version__
from unknown_agent.web.auth import WebAuthStore

log = get_logger(__name__)

COOKIE = "ua_session"
CHAT_SYSTEM = (
    "You are Unknown Agent, a senior engineering assistant. Be concise, accurate and "
    "practical. Answer in the user's language. When code is requested, use fenced blocks."
)
HISTORY_LIMIT = 60

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class _Body(BaseModel):
    token: str = ""
    code: str = ""
    text: str = ""
    name: str = ""
    id: str = ""


# --------------------------------------------------------------------------- #
# rate limiting (tiny, in-memory; good enough for a personal panel)
# --------------------------------------------------------------------------- #
class _Limiter:
    def __init__(self, max_hits: int = 10, window_s: float = 60.0) -> None:
        self.max_hits = max_hits
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
        if len(hits) >= self.max_hits:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


# --------------------------------------------------------------------------- #
# app factory
# --------------------------------------------------------------------------- #
def create_app(*, auth: WebAuthStore, paths: AppPaths, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Unknown Agent", version=__version__, docs_url=None, redoc_url=None)
    limiter = _Limiter()
    history: dict[str, list[dict[str, str]]] = {}

    # -- helpers ------------------------------------------------------------ #
    def _settings() -> Settings:
        return load_settings(paths=paths).settings

    def _client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "?"

    def _session_of(request: Request) -> tuple[str, dict[str, Any]] | None:
        sid = request.cookies.get(COOKIE, "")
        sess = auth.session(sid)
        return (sid, sess) if sess else None

    def _set_cookie(response: JSONResponse, sid: str) -> None:
        response.set_cookie(
            COOKIE,
            sid,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def _unauth() -> JSONResponse:
        return JSONResponse({"error": "authentication required"}, status_code=401)

    def _profile_pair(settings: Settings) -> tuple[str, Any]:
        name = active_profile_name(settings)
        return name, settings.providers.get(name)

    def _provider_view(settings: Settings) -> dict[str, Any]:
        active_name, active = _profile_pair(settings)
        return {
            "version": __version__,
            "active": active_name,
            "model": getattr(active, "model", "") or "" if active else "",
            "providers": [
                {
                    "name": name,
                    "type": getattr(p, "type", "") or "",
                    "model": getattr(p, "model", "") or "",
                    "active": name == active_name,
                }
                for name, p in (settings.providers or {}).items()
            ],
        }

    # -- chat core ----------------------------------------------------------- #
    async def _emit_command_result(emit: Emit, content: str) -> None:
        await emit({"type": "message", "role": "assistant", "content": content, "command": True})
        await emit({"type": "done", "content": content})

    async def _run_command(sid: str, text: str, emit: Emit) -> bool:
        """Handle slash commands.  Returns True when the text was a command."""
        if not text.startswith("/"):
            return False
        parts = text[1:].split(None, 1)
        name = (parts[0] if parts else "").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        settings = _settings()
        content = ""

        if name in {"help", "?"}:
            rows = [
                "/help — this list",
                "/status — provider, model, version",
                "/providers — list configured profiles",
                "/provider <name> — switch active profile",
                "/models — list models the active provider offers",
                "/model <id> — set the active model",
                "/clear — forget this conversation",
            ]
            content = "\n".join(rows)
        elif name == "status":
            view = _provider_view(settings)
            content = (
                f"version {view['version']}\nactive provider: {view['active']}\n"
                f"model: {view['model'] or '(unset)'}"
            )
        elif name == "providers":
            rows = [
                f"{'* ' if p['active'] else '  '}{p['name']}  [{p['type']}]  {p['model']}"
                for p in _provider_view(settings)["providers"]
            ]
            content = "\n".join(rows) or "no providers configured — run ua setup"
        elif name == "provider":
            if not arg:
                content = "usage: /provider <name>"
            elif arg not in (settings.providers or {}):
                content = f"no profile named '{arg}' — try /providers"
            else:
                settings.provider = arg
                save_settings(settings, paths.user_config_file)
                content = f"active provider is now '{arg}'"
        elif name == "models":
            content = await _list_models(settings)
        elif name == "model":
            if not arg:
                content = "usage: /model <id>"
            else:
                pname, profile = _profile_pair(settings)
                if profile is None:
                    content = "no active profile — run ua setup"
                else:
                    profile.model = arg
                    save_settings(settings, paths.user_config_file)
                    content = f"model for '{pname}' is now '{arg}'"
        elif name == "clear":
            history.pop(sid, None)
            content = "conversation cleared"
        else:
            content = f"unknown command '/{name}' — /help lists what this panel understands"

        await _emit_command_result(emit, content)
        return True

    async def _list_models(settings: Settings) -> str:
        from unknown_agent.providers.catalog import known_model_ids
        from unknown_agent.providers.discovery import fetch_models

        pname, profile = _profile_pair(settings)
        if profile is None:
            return "no active profile — run ua setup"
        try:
            models = await asyncio.wait_for(fetch_models(profile), timeout=8.0)
            rows = [m.id for m in models][:40]
            src = "live"
        except Exception:
            rows = list(known_model_ids(pname))[:40]
            src = "catalog (provider unreachable)"
        return "\n".join(rows) or f"no models listed ({src})"

    async def _run_chat(sid: str, text: str, emit: Emit) -> None:
        if await _run_command(sid, text, emit):
            return
        settings = _settings()
        pname, profile = _profile_pair(settings)
        if profile is None:
            await emit(
                {
                    "type": "error",
                    "error": "no provider configured — run `ua setup` on the server, then retry",
                }
            )
            return
        try:
            from unknown_agent.providers.registry import build_provider

            provider = build_provider(profile, name=pname)
        except Exception as exc:
            await emit({"type": "error", "error": f"provider attach failed: {exc}"})
            return

        stored = history.setdefault(sid, [])
        stored.append({"role": "user", "content": text})
        messages = [Message(role="system", content=CHAT_SYSTEM)]
        messages += [Message(role=m["role"], content=m["content"]) for m in stored[-HISTORY_LIMIT:]]
        model = getattr(profile, "model", "") or provider.model
        request = ModelRequest(
            messages=messages,
            temperature=float(getattr(profile, "temperature", provider.temperature) or 0.7),
            max_output_tokens=int(
                getattr(profile, "max_output_tokens", provider.max_output_tokens) or 2048
            ),
        )
        if model:
            provider.model = model

        full = ""
        try:
            if getattr(provider, "supports_streaming", False):
                async for chunk in provider.stream(request):
                    if chunk.error:
                        raise RuntimeError(chunk.error)
                    if chunk.delta:
                        full += chunk.delta
                        await emit({"type": "delta", "delta": chunk.delta})
            else:
                response = await provider.complete(request)
                if response.error:
                    raise RuntimeError(response.error)
                full = response.content or ""
                await emit({"type": "delta", "delta": full})
        except Exception as exc:
            log.debug("web chat error", exc_info=True)
            if full:
                stored.append({"role": "assistant", "content": full})
            await emit({"type": "error", "error": str(exc)})
            return
        stored.append({"role": "assistant", "content": full})
        await emit({"type": "done", "content": full, "model": model, "provider": pname})

    # -- auth endpoints ------------------------------------------------------ #
    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    @app.get("/api/session")
    async def session(request: Request) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return JSONResponse({"authenticated": False})
        _, sess = pair
        return JSONResponse(
            {"authenticated": True, "role": sess.get("role"), "device": sess.get("device")}
        )

    @app.post("/api/auth/login")
    async def login(request: Request, body: _Body) -> JSONResponse:
        if not limiter.allow(_client_ip(request)):
            return JSONResponse({"error": "too many attempts"}, status_code=429)
        if not auth.verify_owner(body.token or ""):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        sid = auth.new_session("owner")
        resp = JSONResponse({"ok": True, "role": "owner"})
        _set_cookie(resp, sid)
        return resp

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        pair = _session_of(request)
        if pair:
            auth.delete_session(pair[0])
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/auth/device/request")
    async def device_request(request: Request) -> JSONResponse:
        if not limiter.allow(_client_ip(request)):
            return JSONResponse({"error": "too many attempts"}, status_code=429)
        label = request.headers.get("user-agent", "")[:120]
        code = auth.request_device(label=label)
        if code is None:
            return JSONResponse({"error": "too many pending requests"}, status_code=429)
        return JSONResponse({"code": code})

    @app.post("/api/auth/device/claim")
    async def device_claim(request: Request, body: _Body) -> JSONResponse:
        code = (body.code or "").strip()
        if not auth.device_approved(code):
            return JSONResponse({"status": "pending"}, status_code=202)
        label = request.headers.get("user-agent", "")[:120]
        sid = auth.new_session("device", device=code, label=label)
        resp = JSONResponse({"status": "approved", "role": "device"})
        _set_cookie(resp, sid)
        return resp

    # -- owner-only management ------------------------------------------------ #
    def _owner(request: Request) -> tuple[str, dict[str, Any]] | None:
        pair = _session_of(request)
        if pair and pair[1].get("role") == "owner":
            return pair
        return None

    @app.get("/api/status")
    async def status(request: Request) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return _unauth()
        return JSONResponse(_provider_view(_settings()))

    @app.get("/api/commands")
    async def commands(request: Request) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return _unauth()
        return JSONResponse(
            [
                {"name": "help", "usage": "/help", "description": "List panel commands"},
                {"name": "status", "usage": "/status", "description": "Provider, model, version"},
                {"name": "providers", "usage": "/providers", "description": "Configured profiles"},
                {"name": "provider", "usage": "/provider <name>", "description": "Switch profile"},
                {"name": "models", "usage": "/models", "description": "Models offered live"},
                {"name": "model", "usage": "/model <id>", "description": "Set active model"},
                {"name": "clear", "usage": "/clear", "description": "Forget this conversation"},
            ]
        )

    @app.get("/api/models")
    async def models(request: Request) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return _unauth()
        from unknown_agent.providers.catalog import known_model_ids
        from unknown_agent.providers.discovery import fetch_models

        pname, profile = _profile_pair(_settings())
        if profile is None:
            return JSONResponse({"models": [], "source": "none"})
        try:
            fetched = await asyncio.wait_for(fetch_models(profile), timeout=8.0)
            return JSONResponse(
                {"models": [m.id for m in fetched][:80], "source": "live", "provider": pname}
            )
        except Exception:
            return JSONResponse(
                {
                    "models": list(known_model_ids(pname))[:80],
                    "source": "catalog",
                    "provider": pname,
                }
            )

    @app.post("/api/provider")
    async def set_provider(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        settings = _settings()
        if body.name not in (settings.providers or {}):
            return JSONResponse({"error": f"no profile named '{body.name}'"}, status_code=404)
        settings.provider = body.name
        save_settings(settings, paths.user_config_file)
        return JSONResponse(_provider_view(settings))

    @app.post("/api/model")
    async def set_model(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        settings = _settings()
        _, profile = _profile_pair(settings)
        if profile is None:
            return JSONResponse({"error": "no active profile"}, status_code=404)
        profile.model = body.id
        save_settings(settings, paths.user_config_file)
        return JSONResponse(_provider_view(settings))

    @app.get("/api/devices")
    async def devices(request: Request) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        return JSONResponse({"pending": auth.pending(), "devices": auth.devices()})

    @app.post("/api/devices/revoke")
    async def revoke(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        if not auth.revoke_device((body.code or "").strip()):
            return JSONResponse({"error": "unknown device"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.post("/api/token/rotate")
    async def rotate(request: Request) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        return JSONResponse({"token": auth.rotate_owner_token()})

    # -- chat ------------------------------------------------------------------ #
    @app.post("/api/chat")
    async def chat(request: Request, body: _Body) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return _unauth()
        sid, _ = pair
        text = (body.text or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        out: dict[str, Any] = {"deltas": [], "content": "", "error": None}

        async def emit(msg: dict[str, Any]) -> None:
            if msg.get("type") == "delta":
                out["deltas"].append(msg.get("delta", ""))
            elif msg.get("type") in {"message", "done"}:
                out["content"] = msg.get("content", "") or out["content"]
            elif msg.get("type") == "error":
                out["error"] = msg.get("error")

        await _run_chat(sid, text, emit)
        if out["error"] and not out["content"]:
            return JSONResponse({"error": out["error"]}, status_code=502)
        return JSONResponse(out)

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket) -> None:
        sid = websocket.cookies.get(COOKIE, "")
        sess = auth.session(sid)
        if not sess:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                payload = await websocket.receive_json()
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue

                async def emit(msg: dict[str, Any]) -> None:
                    await websocket.send_json(msg)

                await _run_chat(sid, text, emit)
        except WebSocketDisconnect:
            return
        except Exception:
            log.debug("web socket error", exc_info=True)

    # -- static / SPA ----------------------------------------------------------- #
    resolved_static = Path(static_dir) if static_dir else None
    if resolved_static and (resolved_static / "index.html").exists():
        assets = resolved_static / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}")
        async def spa(full_path: str) -> Any:
            if full_path.startswith("api"):
                return JSONResponse({"error": "not found"}, status_code=404)
            candidate = (resolved_static / full_path).resolve()
            if (
                full_path
                and not full_path.startswith("api")
                and candidate.is_relative_to(resolved_static.resolve())
                and candidate.is_file()
            ):
                return FileResponse(candidate)
            return FileResponse(resolved_static / "index.html")

    else:

        @app.get("/")
        async def no_ui() -> HTMLResponse:
            return HTMLResponse(
                "<h1>Unknown Agent</h1><p>Web UI assets missing; API is live at /api.</p>"
            )

    return app


__all__ = ["COOKIE", "create_app"]
