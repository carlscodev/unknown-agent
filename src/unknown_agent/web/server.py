"""FastAPI backend for the web control panel.

Design notes
------------
* **Auth** — owner token printed by ``ua web`` (or ``ua web --show-token``), or
  device pairing: the browser shows a 6-digit code, the terminal approves it with
  ``ua approve device <code>``.  Sessions are HttpOnly cookies; the shared 0600
  auth file is the IPC channel (see :mod:`web.auth`).
* **Chat** — one WebSocket per browser.  Every message runs through
  :class:`~unknown_agent.web.agent_loop.WebAgent`: the real tool registry,
  policy engine and executor of the CLI, driven by a streaming tool-calling
  loop.  Visible text (``delta``), internal reasoning (``reasoning``), tool
  calls (``tool``) and written files (``artifact``) are separate live channels,
  and generation is cancellable (``{"type": "stop"}`` / ``POST /api/stop``).
  The bolt button still routes to the full ``Runner`` (planning + verification).
* **Attachments** — uploads land in ``<data>/web_uploads``; text/PDF content is
  extracted into the prompt, images ride ``Message.images`` (native vision parts),
  everything else is referenced by path for agent tooling.
* **Dashboard** — providers, models, agent policy, MCP servers, file options and
  usage, all persisted through the real settings file.
* **Static** — the built Vue 3 app in ``web/static`` with an SPA fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from unknown_agent.config.loader import load_settings, save_settings
from unknown_agent.config.models import McpServerConfig, ProviderConfig, Settings
from unknown_agent.config.paths import AppPaths
from unknown_agent.observability.logging import get_logger
from unknown_agent.tui.provider_status import active_profile_name
from unknown_agent.version import __version__
from unknown_agent.web.agent_loop import WebAgent
from unknown_agent.web.attachments import AttachmentStore
from unknown_agent.web.auth import WebAuthStore
from unknown_agent.web.missions import build_mission_router

log = get_logger(__name__)

COOKIE = "ua_session"
CHAT_SYSTEM = (
    "You are Unknown Agent, a senior engineering assistant. Be concise, accurate and "
    "practical. Answer in the user's language. When code is requested, use fenced blocks."
)
HISTORY_LIMIT = 60
DEFAULT_FILES_OPTS = {"max_upload_mb": 25, "auto_extract": True}

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class _Body(BaseModel):
    token: str = ""
    code: str = ""
    text: str = ""
    name: str = ""
    id: str = ""
    attachments: list[str] = []
    type: str = ""
    model: str = ""
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool = True
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    provider: str | None = None
    mode: str | None = None
    max_steps: int | None = None
    reasoning: str | None = None
    max_upload_mb: int | None = None
    auto_extract: bool | None = None
    agent: bool | None = None


def _multipart_ok() -> bool:
    """python-multipart ships as a core dep; this only guards broken installs."""
    try:
        import multipart  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        import python_multipart  # noqa: F401

        return True
    except ImportError:
        return False


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


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def create_app(*, auth: WebAuthStore, paths: AppPaths, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Unknown Agent", version=__version__, docs_url=None, redoc_url=None)
    limiter = _Limiter()
    history: dict[str, list[dict[str, str]]] = {}
    running: dict[str, asyncio.Task[None]] = {}
    #: cooperative stop flags, checked by the tool loop between steps
    stop_flags: dict[str, dict[str, bool]] = {}
    #: one WebAgent per panel process; rebuilt when the active profile changes
    _agent_cache: dict[str, WebAgent] = {}
    _emit_tasks: set[asyncio.Task[None]] = set()
    files_opts_path = paths.config_dir / "web_files.json"
    usage_path = paths.data_dir / "web_usage.json"
    uploads = AttachmentStore(paths.data_dir / "web_uploads")
    from unknown_agent.clients import ClientError, ClientSyncService, SyncRequest
    from unknown_agent.gateway.devices import DeviceController, DeviceError

    device_controller = DeviceController(paths.project_dir / "gateway.db")
    client_sync = ClientSyncService(paths.project_dir / "clients.db")

    # -- helpers ------------------------------------------------------------ #
    def _settings() -> Settings:
        return load_settings(paths=paths).settings

    def _files_opts() -> dict[str, Any]:
        opts = dict(DEFAULT_FILES_OPTS)
        opts.update(_read_json(files_opts_path, {}))
        return opts

    def _usage() -> dict[str, Any]:
        data = _read_json(usage_path, {})
        data.setdefault("requests", 0)
        data.setdefault("chars", 0)
        data.setdefault("uploads", 0)
        data.setdefault("commands", 0)
        return data

    def _bump_usage(**delta: int) -> None:
        data = _usage()
        for key, val in delta.items():
            data[key] = int(data.get(key, 0)) + val
        _write_json(usage_path, data)

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
            COOKIE, sid, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", path="/"
        )

    def _unauth() -> JSONResponse:
        return JSONResponse({"error": "authentication required"}, status_code=401)

    def _owner(request: Request) -> tuple[str, dict[str, Any]] | None:
        pair = _session_of(request)
        return pair if pair and pair[1].get("role") == "owner" else None

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
                    "base_url": getattr(p, "base_url", None),
                    "active": name == active_name,
                }
                for name, p in (settings.providers or {}).items()
            ],
        }

    def _skill_set(settings: Settings) -> Any:
        try:
            from unknown_agent.skills.loader import resolve_skills

            return resolve_skills(settings, user_dir=paths.user_skills_dir)
        except Exception:
            log.debug("web: skill pack resolve failed", exc_info=True)
            return None

    # -- chat core ----------------------------------------------------------- #
    async def _run_command(sid: str, text: str, emit: Emit) -> bool:
        if not text.startswith("/"):
            return False
        parts = text[1:].split(None, 1)
        name = (parts[0] if parts else "").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        settings = _settings()
        content = ""

        if name in {"help", "?"}:
            content = "\n".join(
                [
                    "/help — this list",
                    "/status — provider, model, version",
                    "/pack — active skill pack and its skills",
                    "/providers — list configured profiles",
                    "/provider <name> — switch active profile",
                    "/models — list models the active provider offers",
                    "/model <id> — set the active model",
                    "/clear — forget this conversation",
                ]
            )
        elif name == "pack":
            skillset = _skill_set(settings)
            if skillset is None or not skillset.skills:
                content = "skill pack disabled or empty (check [skills] pack in config)"
            else:
                rows = [f"packs: {', '.join(skillset.packs) or 'default'}"]
                rows += [f"  - {s.id}" for s in skillset.skills]
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
                _, profile = _profile_pair(settings)
                if profile is None:
                    content = "no active profile — run ua setup"
                else:
                    profile.model = arg
                    save_settings(settings, paths.user_config_file)
                    content = f"model for the active profile is now '{arg}'"
        elif name == "clear":
            history.pop(sid, None)
            content = "conversation cleared"
        else:
            content = f"unknown command '/{name}' — /help lists what this panel understands"

        _bump_usage(commands=1)
        await emit({"type": "message", "role": "assistant", "content": content, "command": True})
        await emit({"type": "done", "content": content})
        return True

    async def _model_ids(pname: str, profile: Any) -> tuple[list[str], str]:
        """Live model list when the gateway answers, catalog otherwise.

        Mirrors the TUI call shape: ``fetch_models`` wants the resolved catalog
        *entry* plus the resolved secret, not the raw ``ProviderConfig``.
        """
        from unknown_agent.config.secrets import resolve_secret
        from unknown_agent.providers.catalog import known_model_ids
        from unknown_agent.providers.discovery import fetch_models, resolve_entry_for

        try:
            entry = resolve_entry_for(profile)
        except Exception:
            entry = None
        if entry is None:
            configured = getattr(profile, "model", "") or ""
            return ([configured] if configured else []), "none"
        api_key = resolve_secret(
            getattr(profile, "api_key", None), key=f"providers.{pname}.api_key"
        )
        try:
            fetched = await asyncio.wait_for(
                fetch_models(entry, api_key=api_key, base_url=getattr(profile, "base_url", None)),
                timeout=8.0,
            )
            return [m.id for m in fetched], "live"
        except Exception:
            return list(known_model_ids(entry)), "catalog"

    async def _list_models(settings: Settings) -> str:
        pname, profile = _profile_pair(settings)
        if profile is None:
            return "no active profile — run ua setup"
        rows, _src = await _model_ids(pname, profile)
        return "\n".join(rows[:40]) or "no models listed"

    async def _run_chat(
        sid: str,
        text: str,
        emit: Emit,
        attach_ids: list[str] | None = None,
        agent_pref: bool | None = None,
        run_provider: str | None = None,
        run_model: str | None = None,
    ) -> None:
        if await _run_command(sid, text, emit):
            return

        # -- attachments become real model context (both modes) ------------- #
        opts = _files_opts()
        blocks: list[str] = []
        images: list[str] = []
        for aid in attach_ids or []:
            meta = uploads.get(aid)
            if meta is None:
                continue
            if meta["kind"] == "image":
                try:
                    images.append(uploads.data_url(meta))
                except OSError:
                    blocks.append(f"[attached image: {meta['name']}] — unreadable file.")
            else:
                blocks.append(uploads.context_block(meta, auto_extract=bool(opts["auto_extract"])))

        user_text = text
        if blocks:
            user_text = f"{text}\n\n" + "\n\n".join(blocks)

        stored = history.setdefault(sid, [])
        stored.append({"role": "user", "content": user_text})

        # -- route: the bolt button asks for the full Runner --------------- #
        # (planning + verification).  Every other message takes the streaming
        # tool loop below, which already has the real tool registry: a build
        # request writes files on this host instead of ending in an apology.
        if agent_pref is True:
            await emit({"type": "mode", "mode": "agent"})
            await _run_agent(
                text, emit, extra="\n\n".join(blocks), provider=run_provider, model=run_model
            )
            return

        await emit({"type": "mode", "mode": "chat"})
        await _run_tool_chat(
            sid,
            text,
            emit,
            images=images,
            extra="\n\n".join(blocks),
            provider=run_provider,
            model=run_model,
        )

    async def _run_tool_chat(
        sid: str,
        text: str,
        emit: Emit,
        *,
        images: list[str] | None = None,
        extra: str = "",
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Stream a tool-using answer: reasoning, text, tool calls, artifacts.

        The model gets the same registry, policy engine and executor the CLI
        uses, so "create a landing page" writes the file on this host instead of
        apologising about missing access.  Everything is streamed as it happens.
        ``provider``/``model`` override the active profile for *this message
        only*: the override lives in the in-memory settings copy, never on disk.
        """

        settings = _settings()
        if provider and provider in (settings.providers or {}):
            settings.provider = provider
        if model and settings.active_provider is not None:
            settings.active_provider.model = model
        if settings.active_provider is None:
            await emit(
                {
                    "type": "error",
                    "error": "no provider configured — run `ua setup` on the server, then retry",
                }
            )
            return

        flag = stop_flags.setdefault(sid, {"stop": False})
        flag["stop"] = False
        stored = history.setdefault(sid, [])
        parts: list[str] = []

        async def relay(msg: dict[str, Any]) -> None:
            if msg.get("type") == "delta":
                parts.append(str(msg.get("delta", "")))
            await emit(msg)

        system_extra = ""
        skillset = _skill_set(settings)
        if skillset is not None:
            try:
                system_extra = skillset.prompt_section(task=text) or ""
            except Exception:  # pragma: no cover - skills are best effort
                system_extra = ""

        try:
            agent = _web_agent(settings)
            await agent.setup()
            if model:
                # belt and braces: the profile carries the override, and the
                # provider instance is forced too so adapters that cached their
                # model at construction time still honour it.
                agent.provider.model = model
            result = await agent.chat(
                task=text,
                emit=relay,
                history=stored[:-1],
                images=images or [],
                extra=extra,
                system_extra=system_extra,
                should_stop=lambda: flag["stop"],
            )
        except asyncio.CancelledError:
            partial = "".join(parts)
            if partial:
                stored.append({"role": "assistant", "content": partial})
            await emit({"type": "stopped", "content": partial})
            raise
        except Exception as exc:
            log.debug("web tool chat failed", exc_info=True)
            partial = "".join(parts)
            if partial:
                stored.append({"role": "assistant", "content": partial})
            await emit({"type": "error", "error": str(exc)})
            return

        if result.content:
            stored.append({"role": "assistant", "content": result.content})
        _bump_usage(requests=1, chars=len(result.content))
        await emit(
            {
                "type": "done",
                "content": result.content,
                "model": result.model,
                "provider": result.provider,
                "timing": result.timing.to_dict(),
                "tools": [tool.to_dict() for tool in result.tools],
                "files": result.files,
                "usage": result.usage,
                "steps": result.steps,
            }
        )

    # -- full agent mode (real tools on this host) ------------------------- #
    # Route a message to the full Runner only when it looks like an imperative
    # build/file task: an action verb at the start AND a file/site noun, or a
    # strong keyword on its own.  Questions and chit-chat keep the fast
    # streaming chat path.
    _agent_starts = (
        "crea",
        "crear",
        "creame",
        "creo",
        "escribe",
        "escribir",
        "genera",
        "generar",
        "haz",
        "hacer",
        "arregla",
        "arreglar",
        "borra",
        "borrar",
        "elimina",
        "mueve",
        "mover",
        "renombra",
        "renombrar",
        "instala",
        "instalar",
        "ejecuta",
        "ejecutar",
        "corre",
        "correr",
        "monta",
        "montar",
        "crea-me",
        "create",
        "write",
        "build",
        "generate",
        "make",
        "fix",
        "delete",
        "remove",
        "move",
        "rename",
        "install",
        "run",
        "scaffold",
        "deploy",
        "set",
        "setup",
    )
    _agent_nouns = (
        "archivo",
        "archivos",
        "fichero",
        "carpeta",
        "carpetas",
        "folder",
        "dir",
        "directorio",
        "script",
        "landing",
        "página",
        "pagina",
        "sitio",
        "site",
        "website",
        "web",
        "html",
        "css",
        "json",
        "csv",
        "api",
        "app",
        "aplicación",
        "aplicacion",
        "programa",
        "bot",
        "server",
        "servidor",
        "test",
        "prueba",
        "file",
        "files",
        "project",
        "proyecto",
    )
    _agent_always = ("landing", "scaffold", "html", "landing page", "pagina web")

    def _needs_agent(text: str) -> bool:
        low = text.lower().strip()
        if any(k in low for k in _agent_always):
            return True
        tokens = low.replace(",", " ").replace(".", " ").split()
        if not tokens:
            return False
        first = tokens[0].rstrip(":;!?")
        if first not in _agent_starts:
            return False
        return any(n in tokens for n in _agent_nouns)

    def _emit_soon(emit: Emit, msg: dict[str, Any]) -> None:
        """Schedule an async emit from the loop's sync hook callbacks."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            return
        task = loop.create_task(emit(msg))
        _emit_tasks.add(task)
        task.add_done_callback(_emit_tasks.discard)

    def _artifact_kind(name: str) -> str:
        low = name.lower()
        if low.endswith((".html", ".htm")):
            return "html"
        if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            return "image"
        if low.endswith(".pdf"):
            return "pdf"
        return "file"

    async def _run_agent(
        text: str,
        emit: Emit,
        extra: str = "",
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Run the real hyperagent (tools, planning, verification) for this message."""
        from urllib.parse import quote

        from unknown_agent.core.loop import LoopHooks
        from unknown_agent.core.runner import Runner, RunRequest

        await emit({"type": "phase", "phase": "agent"})
        loaded = load_settings(paths=paths)
        stop_flag = {"stop": False}

        def thought(t: str) -> None:
            _emit_soon(emit, {"type": "thought", "text": str(t)[:400]})

        def tool_start(call: Any) -> None:
            _emit_soon(
                emit,
                {"type": "tool", "name": getattr(call, "name", "?"), "status": "start"},
            )

        def tool_end(call: Any, result: Any) -> None:
            _emit_soon(
                emit,
                {
                    "type": "tool",
                    "name": getattr(call, "name", "?"),
                    "status": "end",
                    "ok": bool(getattr(result, "ok", True)),
                    "summary": str(getattr(result, "summary", "") or "")[:300],
                },
            )

        def verify(report: Any) -> None:
            _emit_soon(emit, {"type": "verify", "ok": bool(getattr(report, "passed", True))})

        hooks = LoopHooks(
            on_thought=thought,
            on_tool_start=tool_start,
            on_tool_end=tool_end,
            on_verification=verify,
            should_stop=lambda: stop_flag["stop"],
            request_plan_approval=lambda _plan: True,
        )
        request = RunRequest(
            task=text,
            non_interactive=True,
            yes=True,
            extra_instructions=extra or None,
            provider=provider or None,
            model=model or None,
        )
        runner = Runner(loaded, request=request, hooks=hooks)
        try:
            await runner.setup()
            result = await runner.execute()
        except asyncio.CancelledError:
            stop_flag["stop"] = True
            await emit({"type": "stopped", "content": ""})
            raise
        except Exception as exc:  # surface the real failure, never hang
            log.debug("web agent run failed", exc_info=True)
            await emit({"type": "error", "error": f"agent run failed: {exc}"})
            return

        # Prefer the model's own final words; fall back to the run summary.
        answer = ""
        outcome = getattr(result, "outcome", None)
        for step in reversed(getattr(outcome, "steps", []) or []):
            if getattr(step.decision, "answer", ""):
                answer = step.decision.answer
                break
        summary = answer or result.summary or "(run finished without a summary)"
        await emit({"type": "delta", "delta": summary})
        for changed in result.files_changed:
            kind = _artifact_kind(changed)
            if kind != "file" or changed.lower().endswith((".md", ".txt")):
                await emit(
                    {
                        "type": "artifact",
                        "kind": kind,
                        "url": f"/api/files/{quote(changed)}",
                        "name": changed,
                    }
                )
        if not result.ok:
            blockers = "; ".join(result.blockers) or str(result.status)
            await emit({"type": "error", "error": f"run not completed: {blockers}"})
            return
        _bump_usage(requests=1, chars=len(summary))
        await emit(
            {
                "type": "done",
                "content": summary,
                "model": str(result.state.model or ""),
                "provider": str(result.state.provider or ""),
            }
        )

    def _spawn(
        sid: str,
        text: str,
        emit: Emit,
        attach_ids: list[str] | None,
        agent_pref: bool | None = None,
        run_provider: str | None = None,
        run_model: str | None = None,
    ) -> asyncio.Task[None]:
        async def _wrap() -> None:
            try:
                await _run_chat(sid, text, emit, attach_ids, agent_pref, run_provider, run_model)
            except asyncio.CancelledError:
                return
            except Exception:
                # Client went away mid-stream (browser closed, stop pressed):
                # the cancel-path emit can then raise; never log a traceback for it.
                log.debug("web chat task ended", exc_info=True)

        task = asyncio.create_task(_wrap())
        running[sid] = task
        task.add_done_callback(lambda _t: running.pop(sid, None))
        return task

    def _web_agent(settings: Settings) -> WebAgent:
        """Return the panel's tool-using agent, rebuilt when the profile changes."""

        candidate = WebAgent(settings=settings, paths=paths)
        cached = _agent_cache.get("panel")
        if cached is not None and cached.fingerprint == candidate.fingerprint:
            return cached
        if cached is not None:
            # best effort: release the old provider client
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(cached.aclose())
        _agent_cache["panel"] = candidate
        return candidate

    def _stop(sid: str) -> bool:
        flag = stop_flags.get(sid)
        if flag is not None:
            flag["stop"] = True
        task = running.get(sid)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    # -- auth endpoints ------------------------------------------------------ #
    @app.post("/v1/client/enroll")
    async def client_enroll(request: Request) -> dict[str, Any]:
        body = await request.body()
        if len(body) > 1_048_576:
            raise HTTPException(status_code=413, detail="request too large")
        try:
            payload = json.loads(body)
            device = device_controller.enroll(
                str(payload["token"]),
                device_id=str(payload["device_id"]),
                name=str(payload["name"]),
                public_key=str(payload["public_key"]),
                capabilities=list(payload["capabilities"]),
                signature=str(payload["signature"]),
            )
            return device.model_dump(mode="json")
        except (KeyError, TypeError, ValueError, DeviceError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/client/sync")
    async def client_synchronize(request: Request) -> dict[str, Any]:
        body = await request.body()
        if len(body) > 1_048_576:
            raise HTTPException(status_code=413, detail="request too large")
        try:
            device = device_controller.verify_request(
                request.headers["x-device-id"],
                timestamp=int(request.headers["x-device-timestamp"]),
                nonce=request.headers["x-device-nonce"],
                body=body,
                signature=request.headers["x-device-signature"],
            )
            sync_request = SyncRequest.model_validate_json(body)
            response = client_sync.sync(device.id, device.capabilities, sync_request)
            return response.model_dump(mode="json")
        except (KeyError, ValueError, DeviceError) as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ClientError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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

    # -- status / chat -------------------------------------------------------- #
    @app.get("/api/status")
    async def status(request: Request) -> JSONResponse:
        if not _session_of(request):
            return _unauth()
        settings = _settings()
        view = _provider_view(settings)
        view["files"] = _files_opts()
        agent = _agent_cache.get("panel")
        view["cwd"] = str(paths.cwd)
        view["tool_count"] = len(agent.tool_names) if agent else 0
        view["tools"] = agent.tool_names if agent else []
        view["read_only"] = bool(settings.policy.read_only)
        return JSONResponse(view)

    @app.get("/api/commands")
    async def commands(request: Request) -> JSONResponse:
        if not _session_of(request):
            return _unauth()
        return JSONResponse(
            [
                {"name": "help", "usage": "/help", "description": "List panel commands"},
                {"name": "status", "usage": "/status", "description": "Provider, model, version"},
                {"name": "pack", "usage": "/pack", "description": "Active skill pack"},
                {"name": "providers", "usage": "/providers", "description": "Configured profiles"},
                {"name": "provider", "usage": "/provider <name>", "description": "Switch profile"},
                {"name": "models", "usage": "/models", "description": "Models offered live"},
                {"name": "model", "usage": "/model <id>", "description": "Set active model"},
                {"name": "clear", "usage": "/clear", "description": "Forget this conversation"},
            ]
        )

    @app.get("/api/models")
    async def models(request: Request) -> JSONResponse:
        if not _session_of(request):
            return _unauth()
        pname, profile = _profile_pair(_settings())
        if profile is None:
            return JSONResponse({"models": [], "source": "none"})
        rows, source = await _model_ids(pname, profile)
        return JSONResponse({"models": rows[:80], "source": source, "provider": pname})

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

    @app.post("/api/stop")
    async def stop(request: Request) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return _unauth()
        return JSONResponse({"stopped": _stop(pair[0])})

    async def upload(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return _unauth()
        opts = _files_opts()
        cap = int(opts["max_upload_mb"]) * 1024 * 1024
        blob = await file.read()
        if len(blob) > cap:
            return JSONResponse(
                {"error": f"file exceeds the {opts['max_upload_mb']} MB limit"}, status_code=413
            )
        meta = uploads.save(blob, file.filename or "upload.bin")
        _bump_usage(uploads=1)
        return JSONResponse(
            {
                "id": meta["id"],
                "name": meta["name"],
                "kind": meta["kind"],
                "size": meta["size"],
                "size_human": meta["size_human"],
            }
        )

    async def upload_unavailable(request: Request) -> JSONResponse:
        if not _session_of(request):
            return _unauth()
        return JSONResponse(
            {
                "error": "uploads unavailable: this install lacks python-multipart; "
                "reinstall unknown-agent to pull it in"
            },
            status_code=501,
        )

    # The ``File(...)`` signature is inspected at *registration* time, so a box
    # without python-multipart would crash ``ua web`` entirely if the upload route
    # were registered unconditionally.  Degrade to a clear 501 instead; the core
    # dependency list installs it, so a normal install never sees this path.
    if _multipart_ok():
        app.add_api_route("/api/upload", upload, methods=["POST"])
    else:
        app.add_api_route("/api/upload", upload_unavailable, methods=["POST"])

    @app.post("/api/chat")
    async def chat(request: Request, body: _Body) -> JSONResponse:
        pair = _session_of(request)
        if not pair:
            return _unauth()
        sid, _ = pair
        text = (body.text or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        out: dict[str, Any] = {"deltas": [], "content": "", "error": None, "artifacts": []}

        async def emit(msg: dict[str, Any]) -> None:
            if msg.get("type") == "delta":
                out["deltas"].append(msg.get("delta", ""))
            elif msg.get("type") == "artifact":
                out["artifacts"].append(msg)
            elif msg.get("type") in {"message", "done", "stopped"}:
                out["content"] = msg.get("content", "") or out["content"]
            elif msg.get("type") == "error":
                out["error"] = msg.get("error")

        await _run_chat(sid, text, emit, body.attachments, body.agent, body.provider, body.model)
        if out["error"] and not out["content"]:
            return JSONResponse({"error": out["error"]}, status_code=502)
        return JSONResponse(out)

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket) -> None:
        sid = websocket.cookies.get(COOKIE, "")
        if not auth.session(sid):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                payload = await websocket.receive_json()
                kind = str(payload.get("type") or "chat")
                if kind == "stop":
                    if _stop(sid):
                        await websocket.send_json({"type": "stopped"})
                    continue
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue

                async def emit(msg: dict[str, Any]) -> None:
                    await websocket.send_json(msg)

                _spawn(
                    sid,
                    text,
                    emit,
                    payload.get("attachments") or [],
                    payload.get("agent") if isinstance(payload.get("agent"), bool) else None,
                    payload.get("provider") or None,
                    payload.get("model") or None,
                )
        except WebSocketDisconnect:
            _stop(sid)
            return
        except Exception:
            log.debug("web socket error", exc_info=True)
            _stop(sid)

    # -- artifact preview: serve files the agent wrote, rooted at cwd ------- #
    # Every previewable type is served inline with the right media type so an
    # <iframe>/<img> renders it; `?download=1` forces a save dialog instead.
    media_types = {
        ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".pdf": "application/pdf",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".xml": "application/xml; charset=utf-8",
        ".py": "text/plain; charset=utf-8",
    }

    @app.get("/api/files/{full_path:path}")
    async def serve_artifact(request: Request, full_path: str) -> Any:
        from urllib.parse import unquote

        if not _session_of(request):
            return _unauth()
        try:
            rel = unquote(full_path)
        except Exception:
            return JSONResponse({"error": "bad path"}, status_code=400)
        root = paths.cwd.resolve()
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        # no-store: previews must always show the latest content the agent wrote
        headers = {"Cache-Control": "no-store"}
        download = request.query_params.get("download") in {"1", "true", "yes"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{candidate.name}"'
        media_type = media_types.get(candidate.suffix.lower())
        return FileResponse(candidate, headers=headers, media_type=media_type)

    # -- dashboard ------------------------------------------------------------- #
    @app.get("/api/dashboard")
    async def dashboard(request: Request) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        settings = _settings()
        skillset = _skill_set(settings)
        return JSONResponse(
            {
                "providers": _provider_view(settings)["providers"],
                "active": _provider_view(settings)["active"],
                "agent": {
                    "mode": settings.agent.mode,
                    "max_steps": settings.agent.max_steps,
                    "reasoning": getattr(settings.reasoning, "default_mode", "") or "",
                },
                "mcp": {
                    "enabled": settings.mcp.enabled,
                    "servers": [
                        {
                            "name": name,
                            "enabled": srv.enabled,
                            "transport": srv.transport,
                            "command": srv.command,
                            "args": list(srv.args),
                            "url": getattr(srv, "url", None),
                        }
                        for name, srv in (settings.mcp.servers or {}).items()
                    ],
                },
                "files": _files_opts(),
                "usage": _usage(),
                "skills": skillset.ids() if skillset else [],
                "devices": {
                    "pending": auth.pending(),
                    "devices": auth.devices(),
                },
            }
        )

    @app.post("/api/dashboard/provider")
    async def dash_provider(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        name = (body.name or "").strip().lower()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            return JSONResponse({"error": "invalid profile name"}, status_code=400)
        settings = _settings()
        try:
            profile = ProviderConfig(
                type=body.type or "openai-compatible",
                model=body.model or "auto",
                base_url=body.base_url or None,
                api_key=body.api_key or None,
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)
        settings.providers[name] = profile
        save_settings(settings, paths.user_config_file)
        return JSONResponse(_provider_view(settings))

    @app.post("/api/dashboard/provider/remove")
    async def dash_provider_remove(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        settings = _settings()
        name = (body.name or "").strip()
        if name not in (settings.providers or {}) or len(settings.providers) <= 1:
            return JSONResponse({"error": "cannot remove that profile"}, status_code=400)
        del settings.providers[name]
        if settings.provider == name:
            settings.provider = next(iter(settings.providers))
        save_settings(settings, paths.user_config_file)
        return JSONResponse(_provider_view(settings))

    @app.post("/api/dashboard/agent")
    async def dash_agent(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        settings = _settings()
        if body.mode in {"auto", "plan", "react"}:
            settings.agent.mode = body.mode
        if body.max_steps is not None and 1 <= body.max_steps <= 200:
            settings.agent.max_steps = int(body.max_steps)
        if body.reasoning:
            settings.reasoning.default_mode = body.reasoning
        save_settings(settings, paths.user_config_file)
        return JSONResponse(
            {
                "mode": settings.agent.mode,
                "max_steps": settings.agent.max_steps,
                "reasoning": settings.reasoning.default_mode,
            }
        )

    @app.post("/api/dashboard/mcp/add")
    async def dash_mcp_add(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        name = (body.name or "").strip().lower()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            return JSONResponse({"error": "invalid server name"}, status_code=400)
        if not (body.command or body.url):
            return JSONResponse({"error": "command or url required"}, status_code=400)
        settings = _settings()
        settings.mcp.servers[name] = McpServerConfig(
            enabled=bool(body.enabled),
            transport="http" if body.url and not body.command else "stdio",
            command=body.command or None,
            args=list(body.args),
            url=body.url or None,
        )
        save_settings(settings, paths.user_config_file)
        return JSONResponse({"ok": True, "servers": list(settings.mcp.servers)})

    @app.post("/api/dashboard/mcp/toggle")
    async def dash_mcp_toggle(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        settings = _settings()
        srv = (settings.mcp.servers or {}).get((body.name or "").strip())
        if srv is None:
            return JSONResponse({"error": "unknown server"}, status_code=404)
        srv.enabled = bool(body.enabled)
        save_settings(settings, paths.user_config_file)
        return JSONResponse({"ok": True, "enabled": srv.enabled})

    @app.post("/api/dashboard/mcp/remove")
    async def dash_mcp_remove(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        settings = _settings()
        if (body.name or "").strip() not in (settings.mcp.servers or {}):
            return JSONResponse({"error": "unknown server"}, status_code=404)
        del settings.mcp.servers[(body.name or "").strip()]
        save_settings(settings, paths.user_config_file)
        return JSONResponse({"ok": True})

    @app.post("/api/dashboard/files")
    async def dash_files(request: Request, body: _Body) -> JSONResponse:
        if not _owner(request):
            return _unauth()
        opts = _files_opts()
        if body.max_upload_mb is not None and 1 <= body.max_upload_mb <= 200:
            opts["max_upload_mb"] = int(body.max_upload_mb)
        if body.auto_extract is not None:
            opts["auto_extract"] = bool(body.auto_extract)
        _write_json(files_opts_path, opts)
        return JSONResponse(opts)

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

    # -- mission control ------------------------------------------------------- #
    # Mounted before the SPA catch-all, which would otherwise swallow /api/missions.
    # The guard reuses the panel's own session check, so mission control is exactly
    # as protected as the rest of the API rather than quietly opening a back door.
    def _mission_guard(request: Request) -> None:
        if _session_of(request) is None:
            raise HTTPException(status_code=401, detail="authentication required")

    try:
        app.include_router(build_mission_router(project_root=paths.cwd, guard=_mission_guard))
    except Exception as exc:
        log.warning("mission control endpoints unavailable: %s", exc)

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
